from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import requests

from .elevation import enrich_graph_open_meteo
from .models import Coordinate, RouteCandidate, RoutePreferences, StartCandidate
from .osm import generate_loops, graph_radius_meters, load_graph
from .scoring import rank_candidates
from .starts import discover_public_starts


@dataclass(frozen=True)
class PlanResult:
    candidates: list[RouteCandidate]
    discovered_starts: int
    fetched_elevation_cells: int
    warnings: tuple[str, ...]


def geocode_address(address: str) -> Coordinate:
    import osmnx as ox

    latitude, longitude = ox.geocoder.geocode(address)
    return Coordinate(float(latitude), float(longitude))


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
        graph = load_graph(
            start.coordinate, graph_radius_meters(preferences), cache_directory
        )
        start_routes = generate_loops(
            graph,
            start.coordinate,
            preferences,
            drive_distance_miles=start.drive_distance_miles,
            elevation_loader=elevation_loader,
        )
        for candidate in start_routes:
            candidate.start = start
        candidates.extend(start_routes)

    ranked = rank_candidates(candidates, preferences)[: preferences.result_count]
    return PlanResult(
        candidates=ranked,
        discovered_starts=max(0, len(starts) - 1),
        fetched_elevation_cells=fetched_cells,
        warnings=tuple(dict.fromkeys(warnings)),
    )
