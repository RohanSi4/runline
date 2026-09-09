from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path
from typing import Any

import networkx as nx

from .coverage import (
    containing_area,
    downloads_allowed,
    load_area_drive_graph,
    load_area_starts,
)
from .geo import distance_meters
from .models import METERS_PER_MILE, Coordinate, StartCandidate
from .osm import _nearest_node


BLOCKED_ACCESS = {"private", "no", "customers", "permit"}
KIND_PRIORITY = {"trailhead": 0, "parking": 1, "park": 2}


def _text_values(value: Any) -> set[str]:
    if value is None:
        return set()
    if isinstance(value, str):
        return {value.lower()}
    try:
        return {str(item).lower() for item in value}
    except TypeError:
        return {str(value).lower()}


def _feature_kind(data: dict[str, Any]) -> str | None:
    if "trailhead" in _text_values(data.get("information")):
        return "trailhead"
    if "parking" in _text_values(data.get("amenity")):
        return "parking"
    if "park" in _text_values(data.get("leisure")):
        return "park"
    return None


def _feature_candidate(
    data: dict[str, Any], geometry: Any, origin: Coordinate, radius_miles: float
) -> StartCandidate | None:
    kind = _feature_kind(data)
    if kind is None or _text_values(data.get("access")) & BLOCKED_ACCESS:
        return None
    point = geometry if geometry.geom_type == "Point" else geometry.representative_point()
    coordinate = Coordinate(float(point.y), float(point.x))
    direct_miles = distance_meters(origin, coordinate) / METERS_PER_MILE
    if direct_miles < 0.15 or direct_miles > radius_miles:
        return None
    raw_name = data.get("name")
    label = str(raw_name) if isinstance(raw_name, str) and raw_name.strip() else kind.title()
    return StartCandidate(label, kind, coordinate, direct_miles)


def load_drive_graph(
    origin: Coordinate, radius_meters: float, cache_directory: Path
) -> nx.Graph:
    import osmnx as ox

    cache_directory.mkdir(parents=True, exist_ok=True)
    ox.settings.use_cache = True
    ox.settings.cache_folder = str(cache_directory / "http")
    digest = hashlib.sha256(
        f"drive,{origin.latitude:.6f},{origin.longitude:.6f},{round(radius_meters)}".encode()
    ).hexdigest()[:16]
    graph_path = cache_directory / f"drive-{digest}.graphml"
    if graph_path.exists():
        return ox.load_graphml(graph_path)
    graph = ox.graph.graph_from_point(
        (origin.latitude, origin.longitude),
        dist=radius_meters,
        network_type="drive",
        simplify=True,
        retain_all=False,
    )
    ox.save_graphml(graph, graph_path)
    return graph


def _road_distance_miles(
    graph: nx.Graph, origin: Coordinate, destination: Coordinate
) -> float | None:
    start = _nearest_node(graph, origin)
    end = _nearest_node(graph, destination)
    try:
        meters = nx.shortest_path_length(graph, start, end, weight="length")
    except (nx.NetworkXNoPath, nx.NodeNotFound):
        return None
    return float(meters) / METERS_PER_MILE


def _starts_from_precomputed(
    entries: tuple,
    area: Any,
    origin: Coordinate,
    radius_miles: float,
    *,
    limit: int,
) -> list[StartCandidate]:
    """Select shipped start candidates near an origin and measure the drive."""

    possible: list[StartCandidate] = []
    for entry in entries:
        coordinate = Coordinate(float(entry["latitude"]), float(entry["longitude"]))
        direct_miles = distance_meters(origin, coordinate) / METERS_PER_MILE
        if direct_miles < 0.15 or direct_miles > radius_miles:
            continue
        if any(
            distance_meters(coordinate, existing.coordinate) < 150
            for existing in possible
        ):
            continue
        possible.append(
            StartCandidate(entry["label"], entry["kind"], coordinate, direct_miles)
        )

    drive_graph = load_area_drive_graph(area)
    if drive_graph is None:
        return sorted(
            possible, key=lambda item: (KIND_PRIORITY[item.kind], item.drive_distance_miles)
        )[:limit]

    reachable: list[StartCandidate] = []
    for candidate in possible:
        road_distance = _road_distance_miles(drive_graph, origin, candidate.coordinate)
        if road_distance is None or road_distance > radius_miles:
            continue
        reachable.append(replace(candidate, drive_distance_miles=road_distance))
    return sorted(
        reachable,
        key=lambda item: (KIND_PRIORITY[item.kind], item.drive_distance_miles),
    )[:limit]


def discover_public_starts(
    origin: Coordinate,
    radius_miles: float,
    cache_directory: Path,
    *,
    limit: int = 5,
) -> list[StartCandidate]:
    """Find drivable public run starts from OSM trailhead/parking/park features."""

    if radius_miles <= 0:
        return []

    area = containing_area(origin)
    if area is not None:
        precomputed = load_area_starts(area)
        if precomputed:
            return _starts_from_precomputed(
                precomputed, area, origin, radius_miles, limit=limit
            )

    if not downloads_allowed():
        # Discovering starts costs two Overpass calls, which cannot complete
        # inside a serverless request. Without precomputed starts the origin
        # is still a perfectly good place to run from.
        return []

    import osmnx as ox

    ox.settings.use_cache = True
    ox.settings.cache_folder = str(cache_directory / "http")
    try:
        features = ox.features.features_from_point(
            (origin.latitude, origin.longitude),
            tags={
                "information": "trailhead",
                "amenity": "parking",
                "leisure": "park",
            },
            dist=radius_miles * METERS_PER_MILE,
        )
    except ox._errors.InsufficientResponseError:
        return []
    possible: list[StartCandidate] = []
    for _, row in features.iterrows():
        candidate = _feature_candidate(
            row.to_dict(), row.geometry, origin, radius_miles
        )
        if candidate is None:
            continue
        if any(
            distance_meters(candidate.coordinate, existing.coordinate) < 150
            for existing in possible
        ):
            continue
        possible.append(candidate)

    drive_graph = load_drive_graph(
        origin, radius_miles * METERS_PER_MILE + 1_000, cache_directory
    )
    reachable: list[StartCandidate] = []
    for candidate in possible:
        road_distance = _road_distance_miles(
            drive_graph, origin, candidate.coordinate
        )
        if road_distance is None or road_distance > radius_miles:
            continue
        reachable.append(replace(candidate, drive_distance_miles=road_distance))
    return sorted(
        reachable,
        key=lambda item: (KIND_PRIORITY[item.kind], item.drive_distance_miles),
    )[:limit]
