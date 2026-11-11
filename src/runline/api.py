from __future__ import annotations

import logging
import os
from pathlib import Path
import tempfile
from typing import Literal

import requests
from fastapi import FastAPI, HTTPException
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, model_validator

from .coverage import CoverageError, MapDataUnavailable, area_labels, areas, load_area_places
from .export import candidate_feature
from .gpx import candidate_to_gpx
from .models import (
    Coordinate,
    ElevationPreference,
    RoutePreferences,
    SurfacePreference,
)
from .planner import geocode_address, plan_routes
from .point_to_point import plan_point_to_point


LOGGER = logging.getLogger("runline.api")

PROJECT_ROOT = Path(__file__).resolve().parents[2]
WEB_ROOT = PROJECT_ROOT / "web"


def remote_api() -> str:
    """Optional regional backend, independent of this site's public URL."""
    return os.environ.get("RUNLINE_REMOTE_API", "").strip().rstrip("/")


def remote_routes(request: "RouteRequest", origin: Coordinate) -> dict:
    # Resolve an address once, then let the regional service use its own exact
    # graph/pools. Never forward to this site's public proxy (which would loop).
    body = request.model_dump(exclude={"address", "mode", "destination"})
    body.update(latitude=origin.latitude, longitude=origin.longitude)
    try:
        response = requests.post(remote_api() + "/api/routes", json=body, timeout=(5, 75))
        payload = response.json()
        if response.status_code == 422:
            raise HTTPException(status_code=422, detail=payload.get("detail") or "No routes available for this start.")
        response.raise_for_status()
        if (not isinstance(payload, dict) or not isinstance(payload.get("origin"), dict)
                or payload.get("source") not in ("pool", "live")
                or not isinstance(payload.get("routes"), list) or not payload["routes"]
                or any(not isinstance(route, dict) or not {"feature", "gpx"} <= route.keys()
                       for route in payload["routes"])):
            raise ValueError("invalid regional route response")
        return payload
    except HTTPException:
        raise
    except (requests.RequestException, ValueError, AttributeError):
        LOGGER.exception("regional route service is unavailable")
        raise HTTPException(status_code=503, detail="The regional route service is unavailable right now. Try again shortly.") from None


def combined_area_labels() -> list[str]:
    labels = list(area_labels())
    if remote_api():
        try:
            response = requests.get(remote_api() + "/api/areas", timeout=(3, 5))
            response.raise_for_status()
            remote = response.json()["areas"]
            if not isinstance(remote, list) or not all(isinstance(label, str) for label in remote):
                raise ValueError("invalid regional coverage response")
            labels.extend(remote)
        except (requests.RequestException, ValueError, KeyError, TypeError):
            # A regional outage must not hide or disable the local dataset.
            LOGGER.warning("regional coverage is unavailable", exc_info=True)
    return list(dict.fromkeys(labels))


def cache_root() -> Path:
    # A server install runs as a user that cannot write the checkout, so it
    # names a directory it owns (deploy/runline.service uses systemd's
    # CacheDirectory) rather than falling through to a shared /tmp.
    override = os.environ.get("RUNLINE_CACHE_DIR")
    if override:
        return Path(override)
    if os.environ.get("VERCEL"):
        return Path(tempfile.gettempdir()) / "runline-cache"
    local_cache = PROJECT_ROOT / "cache"
    try:
        local_cache.mkdir(parents=True, exist_ok=True)
    except OSError:
        return Path(tempfile.gettempdir()) / "runline-cache"
    return local_cache


CACHE_ROOT = cache_root()

app = FastAPI(title="Runline", version="0.1.0")
app.mount("/assets", StaticFiles(directory=WEB_ROOT), name="assets")


class LocationRequest(BaseModel):
    address: str | None = Field(default=None, max_length=300)
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)

    @model_validator(mode="after")
    def has_start(self) -> "LocationRequest":
        if self.address and self.address.strip():
            return self
        if self.latitude is None or self.longitude is None:
            raise ValueError("provide an address or a map location")
        return self


class RouteRequest(LocationRequest):
    mode: Literal["loop", "point_to_point"] = "loop"
    destination: LocationRequest | None = None
    distance_miles: float | None = Field(default=None, gt=0, le=30)
    drive_radius_miles: Literal[0, 1, 3, 5] = 0
    surface: Literal["road", "mixed", "trail"] = "mixed"
    elevation: Literal["flat", "balanced", "hilly"] = "balanced"
    result_count: int = Field(default=3, ge=1, le=5)

    @model_validator(mode="after")
    def valid_mode(self) -> "RouteRequest":
        if self.mode == "loop" and self.distance_miles is None:
            raise ValueError("choose a distance for your loop")
        if self.mode == "point_to_point":
            if self.destination is None:
                raise ValueError("choose a finish location")
            if self.drive_radius_miles:
                raise ValueError("nearby starts are only available for loops")
            if self.distance_miles is not None or self.elevation != "balanced":
                raise ValueError("distance and elevation preferences are only available for loops")
        return self


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(WEB_ROOT / "index.html")


@app.get("/api/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/version")
async def version() -> dict[str, str]:
    """Identify the API release independently of the proxied frontend."""

    return {
        "version": app.version,
        "commit": os.environ.get("RUNLINE_BUILD_SHA") or os.environ.get("VERCEL_GIT_COMMIT_SHA") or "unknown",
    }


@app.post("/api/routes")
async def routes(request: RouteRequest) -> dict:
    try:
        if request.address and request.address.strip():
            try:
                origin = await run_in_threadpool(
                    geocode_address, request.address.strip(), CACHE_ROOT
                )
            except ValueError as error:
                # An address Nominatim cannot place is a user error, not a fault.
                raise HTTPException(status_code=422, detail=str(error)) from error
        else:
            origin = Coordinate(float(request.latitude), float(request.longitude))
        preferences = RoutePreferences(
            target_distance_miles=request.distance_miles or 1,
            drive_radius_miles=request.drive_radius_miles,
            surface=SurfacePreference(request.surface),
            elevation=ElevationPreference(request.elevation),
            result_count=request.result_count,
        )
        if request.mode == "point_to_point":
            finish = request.destination
            if finish.address and finish.address.strip():
                try:
                    destination = await run_in_threadpool(geocode_address, finish.address.strip(), CACHE_ROOT)
                except ValueError as error:
                    raise HTTPException(status_code=422, detail=str(error)) from error
            else:
                destination = Coordinate(float(finish.latitude), float(finish.longitude))
            result = await run_in_threadpool(
                plan_point_to_point, origin, destination, preferences,
                cache_directory=CACHE_ROOT,
            )
        else:
            result = await run_in_threadpool(
                plan_routes,
                origin,
                preferences,
                cache_directory=CACHE_ROOT,
                start_candidate_limit=2,
            )
    except HTTPException:
        raise
    except MapDataUnavailable as error:
        # A covered tile whose graph could not be fetched: temporary, not a fault.
        raise HTTPException(status_code=503, detail=str(error)) from error
    except CoverageError as error:
        # The legacy regional service ignores unknown fields. Forwarding a
        # destination there could silently return a loop instead of a path.
        if request.mode == "loop" and remote_api():
            return await run_in_threadpool(remote_routes, request, origin)
        # Expected outcome for an unsupported location, not a fault.
        raise HTTPException(status_code=422, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except requests.RequestException as error:
        LOGGER.exception("upstream request failed while planning routes")
        raise HTTPException(
            status_code=503,
            detail="A map data provider is unavailable right now. Try again shortly.",
        ) from error
    except Exception as error:
        # Log the traceback: returning str(error) alone leaves nothing to debug.
        LOGGER.exception("route planning failed")
        raise HTTPException(
            status_code=500,
            detail="Route generation failed unexpectedly.",
        ) from error

    if not result.candidates:
        raise HTTPException(
            status_code=422,
            detail="No viable loops found. Try another distance or surface preference.",
        )
    actual_origin = result.candidates[0].coordinates[0] if request.mode == "point_to_point" else origin
    response = {
        "mode": request.mode,
        "origin": {"latitude": actual_origin.latitude, "longitude": actual_origin.longitude},
        "discovered_starts": result.discovered_starts,
        "source": result.source,
        "warnings": result.warnings,
        "routes": [
            {
                "feature": candidate_feature(candidate),
                "gpx": candidate_to_gpx(
                    candidate, name=f"Runline option {index}"
                ),
            }
            for index, candidate in enumerate(result.candidates, start=1)
        ],
    }
    if request.mode == "point_to_point":
        actual_finish = result.candidates[0].coordinates[-1]
        response["destination"] = {"latitude": actual_finish.latitude, "longitude": actual_finish.longitude}
    return response


@app.get("/api/areas")
async def supported_areas() -> dict:
    return {"areas": await run_in_threadpool(combined_area_labels)}


@app.get("/api/capabilities")
async def capabilities() -> dict:
    return {"modes": {"loop": {"areas": await run_in_threadpool(combined_area_labels)},
                       "point_to_point": {"areas": [area.label for area in areas()]}}}


@app.get("/api/places")
async def places() -> JSONResponse:
    """Autocomplete entries for every covered area.

    Served from the shipped map rather than a geocoder: Nominatim's usage
    policy forbids per-keystroke autocomplete. The list is immutable for a
    given deployment, so it is cached hard in the browser.
    """

    entries: list[dict] = []
    for area in areas():
        entries.extend(load_area_places(area))
    return JSONResponse(
        {"places": entries},
        headers={"Cache-Control": "public, max-age=86400, immutable"},
    )


def run() -> None:
    import uvicorn

    uvicorn.run("runline.api:app", host="127.0.0.1", port=8765, reload=True)
