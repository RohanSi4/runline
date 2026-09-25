from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

from .coverage import CoverageError
from .elevation import enrich_graph_open_meteo
from .models import Coordinate, RouteCandidate, RoutePreferences, StartCandidate
from .osm import generate_loops, graph_radius_meters, prepare_core
from .scoring import rank_candidates
from .starts import discover_public_starts


@dataclass(frozen=True)
class PlanResult:
    candidates: list[RouteCandidate]
    discovered_starts: int
    fetched_elevation_cells: int
    warnings: tuple[str, ...]


NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
# Nominatim's usage policy requires an identifying User-Agent.
NOMINATIM_HEADERS = {"User-Agent": "runline/0.1 (+https://runline-nine.vercel.app)"}


def geocode_address(
    address: str, cache_directory: Path | None = None, *, session: Any = requests
) -> Coordinate:
    """Resolve an address with Nominatim.

    This calls the service directly rather than through osmnx, which pulls in
    geopandas, pandas, pyogrio, pyproj and shapely. That is roughly 70MB a
    serverless instance downloads on every cold start, and none of it is
    needed to serve a precomputed area.
    """

    response = session.get(
        NOMINATIM_URL,
        params={"q": address, "format": "json", "limit": 1},
        headers=NOMINATIM_HEADERS,
        timeout=20,
    )
    response.raise_for_status()
    payload = response.json()
    if not payload:
        raise ValueError(f"could not find a location for {address!r}")
    return Coordinate(float(payload[0]["lat"]), float(payload[0]["lon"]))


def plan_routes(
    origin: Coordinate,
    preferences: RoutePreferences,
    *,
    cache_directory: Path,
    elevation_source: str = "open-meteo",
    start_candidate_limit: int = 5,
) -> PlanResult:
    fetched_cells = 0
    warnings: list[str] = []

    def load_elevation(route_graph, node_ids) -> None:
        nonlocal fetched_cells
        try:
            fetched_cells += enrich_graph_open_meteo(
                route_graph,
                cache_directory / "elevation" / "open-meteo.json",
                node_ids=node_ids,
            )
        except (requests.RequestException, ValueError) as error:
            warnings.append(f"Elevation unavailable: {error}")

    elevation_loader = load_elevation if elevation_source == "open-meteo" else None
    starts = [StartCandidate("Start here", "origin", origin, 0)]
    if preferences.drive_radius_miles:
        starts.extend(
            discover_public_starts(
                origin,
                preferences.drive_radius_miles,
                cache_directory,
                limit=start_candidate_limit,
            )
        )

    candidates: list[RouteCandidate] = []
    for start in starts:
        try:
            graph = prepare_core(
                start.coordinate,
                graph_radius_meters(preferences),
                cache_directory,
                preferences,
            )
        except CoverageError as error:
            # The origin must be mapped, but a discovered start near the edge
            # of a shipped area need not be. Skip it instead of failing a
            # request that still has a usable start.
            if start.kind == "origin":
                raise
            warnings.append(f"Skipped {start.label}: {error}")
            continue
        # Precomputed graphs ship with elevation already attached; refetching it
        # per request is pure latency, and on a serverless host the Open-Meteo
        # cache is wiped between instances so it would never amortise.
        start_loader = None if graph.graph.get("elevation_baked") else elevation_loader
        if graph.graph.get("coverage_limited"):
            warnings.append(f"Map coverage is limited near {start.label}; routes may use fewer streets.")
        start_routes = generate_loops(
            graph,
            start.coordinate,
            preferences,
            drive_distance_miles=start.drive_distance_miles,
            elevation_loader=start_loader,
        )
        for candidate in start_routes:
            candidate.start = start
        candidates.extend(start_routes)

    ranked = rank_candidates(candidates, preferences)[: preferences.result_count]
    if ranked and not any(
        abs(candidate.metrics.distance_miles - preferences.target_distance_miles)
        <= preferences.distance_tolerance_miles
        for candidate in ranked
    ):
        warnings.append("No route is within the requested distance tolerance; showing the closest available options.")
    return PlanResult(
        candidates=ranked,
        discovered_starts=max(0, len(starts) - 1),
        fetched_elevation_cells=fetched_cells,
        warnings=tuple(dict.fromkeys(warnings)),
    )
