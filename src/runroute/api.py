from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, model_validator

from .export import candidate_feature
from .gpx import candidate_to_gpx
from .models import (
    Coordinate,
    ElevationPreference,
    RoutePreferences,
    SurfacePreference,
)
from .planner import geocode_address, plan_routes


PROJECT_ROOT = Path(__file__).resolve().parents[2]
WEB_ROOT = PROJECT_ROOT / "web"


def cache_root() -> Path:
    if os.environ.get("VERCEL"):
        return Path(tempfile.gettempdir()) / "run-route-lab-cache"
    local_cache = PROJECT_ROOT / "cache"
    try:
        local_cache.mkdir(parents=True, exist_ok=True)
    except OSError:
        return Path(tempfile.gettempdir()) / "run-route-lab-cache"
    return local_cache


CACHE_ROOT = cache_root()

app = FastAPI(title="Runline", version="0.1.0")
app.mount("/assets", StaticFiles(directory=WEB_ROOT), name="assets")


class RouteRequest(BaseModel):
    address: str | None = Field(default=None, max_length=300)
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    distance_miles: float = Field(gt=0, le=30)
    drive_radius_miles: Literal[0, 1, 3, 5] = 0
    surface: Literal["road", "mixed", "trail"] = "mixed"
    elevation: Literal["flat", "balanced", "hilly"] = "balanced"
    result_count: int = Field(default=3, ge=1, le=5)

    @model_validator(mode="after")
    def has_start(self) -> "RouteRequest":
        if self.address and self.address.strip():
            return self
        if self.latitude is None or self.longitude is None:
            raise ValueError("provide an address or a map location")
        return self


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(WEB_ROOT / "index.html")


@app.get("/api/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/routes")
async def routes(request: RouteRequest) -> dict:
    try:
        if request.address and request.address.strip():
            origin = await run_in_threadpool(
                geocode_address, request.address.strip(), CACHE_ROOT
            )
        else:
            origin = Coordinate(float(request.latitude), float(request.longitude))
        preferences = RoutePreferences(
            target_distance_miles=request.distance_miles,
            drive_radius_miles=request.drive_radius_miles,
            surface=SurfacePreference(request.surface),
            elevation=ElevationPreference(request.elevation),
            result_count=request.result_count,
        )
        result = await run_in_threadpool(
            plan_routes,
            origin,
            preferences,
            cache_directory=CACHE_ROOT,
            start_candidate_limit=2,
        )
    except Exception as error:
        raise HTTPException(status_code=503, detail=str(error)) from error

    if not result.candidates:
        raise HTTPException(
            status_code=422,
            detail="No viable loops found. Try another distance or surface preference.",
        )
    return {
        "origin": {"latitude": origin.latitude, "longitude": origin.longitude},
        "discovered_starts": result.discovered_starts,
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


@app.get("/api/demo")
async def demo() -> dict:
    demo_directory = PROJECT_ROOT / "output" / "legacy-3-drive-1"
    geojson_path = demo_directory / "routes.geojson"
    if not geojson_path.exists():
        raise HTTPException(status_code=404, detail="Demo routes have not been generated.")
    collection = json.loads(geojson_path.read_text(encoding="utf-8"))
    routes = []
    starts: set[str] = set()
    for index, feature in enumerate(collection["features"], start=1):
        start = feature["properties"].get("start") or {}
        if start.get("kind") != "origin" and start.get("label"):
            starts.add(start["label"])
        routes.append(
            {
                "feature": feature,
                "gpx": (demo_directory / f"option-{index}.gpx").read_text(
                    encoding="utf-8"
                ),
            }
        )
    return {
        "origin": {"latitude": 38.9798655, "longitude": -77.5257475},
        "discovered_starts": len(starts),
        "warnings": [],
        "routes": routes,
    }


def run() -> None:
    import uvicorn

    uvicorn.run("runroute.api:app", host="127.0.0.1", port=8765, reload=True)
