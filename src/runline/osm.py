from __future__ import annotations

import hashlib
import math
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Callable

import networkx as nx

from .geo import destination
from .models import (
    METERS_PER_MILE,
    Coordinate,
    RouteCandidate,
    RouteMetrics,
    RoutePreferences,
    SurfacePreference,
)
from .scoring import rank_candidates


TRAIL_HIGHWAYS = {"bridleway", "footway", "path", "track"}
MAJOR_HIGHWAYS = {"motorway", "trunk", "primary", "secondary"}
UNPAVED_SURFACES = {
    "compacted",
    "dirt",
    "earth",
    "fine_gravel",
    "gravel",
    "ground",
    "mud",
    "sand",
    "unpaved",
    "woodchips",
}


def _values(value: Any) -> set[str]:
    if value is None:
        return set()
    if isinstance(value, str):
        return {value.lower()}
    if isinstance(value, Iterable):
        return {str(item).lower() for item in value}
    return {str(value).lower()}


def _is_trail(data: dict[str, Any]) -> bool:
    return bool(_values(data.get("highway")) & TRAIL_HIGHWAYS) or bool(
        _values(data.get("surface")) & UNPAVED_SURFACES
    )


def _is_major(data: dict[str, Any]) -> bool:
    return bool(_values(data.get("highway")) & MAJOR_HIGHWAYS)


def _is_signal(node_data: dict[str, Any]) -> bool:
    return "traffic_signals" in _values(node_data.get("highway")) or (
        "traffic_signals" in _values(node_data.get("crossing"))
    ) or "yes" in _values(node_data.get("crossing:signals"))


def _edge_preference_cost(
    data: dict[str, Any],
    destination_node: dict[str, Any],
    preferences: RoutePreferences,
) -> float:
    length = float(data.get("length", 1.0))
    cost = length

    if _is_signal(destination_node):
        cost += 450.0
    if _is_major(data):
        cost += max(120.0, length * 1.8)

    trail = _is_trail(data)
    if preferences.surface is SurfacePreference.TRAIL and not trail:
        cost += length * 0.65
    elif preferences.surface is SurfacePreference.ROAD and trail:
        cost += length * 0.8

    return cost


def _iter_edges(graph: nx.Graph):
    if graph.is_multigraph():
        yield from graph.edges(keys=True, data=True)
    else:
        for u, v, data in graph.edges(data=True):
            yield u, v, 0, data


def collapse_graph(graph: nx.Graph, preferences: RoutePreferences) -> nx.DiGraph:
    """Collapse parallel OSM edges to the cheapest runner-specific edge."""

    collapsed = nx.DiGraph()
    collapsed.graph.update(graph.graph)
    collapsed.add_nodes_from(graph.nodes(data=True))
    for u, v, _, raw_data in _iter_edges(graph):
        data = dict(raw_data)
        data["_cost"] = _edge_preference_cost(data, graph.nodes[v], preferences)
        existing = collapsed.get_edge_data(u, v)
        if existing is None or data["_cost"] < existing["_cost"]:
            collapsed.add_edge(u, v, **data)
    return collapsed


def _edge_geometry_coordinates(
    graph: nx.DiGraph, u: int, v: int
) -> list[Coordinate]:
    data = graph.edges[u, v]
    geometry = data.get("geometry")
    if geometry is None:
        return [
            Coordinate(float(graph.nodes[u]["y"]), float(graph.nodes[u]["x"])),
            Coordinate(float(graph.nodes[v]["y"]), float(graph.nodes[v]["x"])),
        ]

    points = [Coordinate(float(lat), float(lon)) for lon, lat in geometry.coords]
    start = Coordinate(float(graph.nodes[u]["y"]), float(graph.nodes[u]["x"]))
    direct = abs(points[0].latitude - start.latitude) + abs(points[0].longitude - start.longitude)
    reverse = abs(points[-1].latitude - start.latitude) + abs(points[-1].longitude - start.longitude)
    return points if direct <= reverse else list(reversed(points))


def _route_coordinates(graph: nx.DiGraph, node_ids: list[int]) -> tuple[Coordinate, ...]:
    coordinates: list[Coordinate] = []
    for u, v in zip(node_ids, node_ids[1:]):
        edge_coordinates = _edge_geometry_coordinates(graph, u, v)
        if coordinates and coordinates[-1] == edge_coordinates[0]:
            coordinates.extend(edge_coordinates[1:])
        else:
            coordinates.extend(edge_coordinates)
    return tuple(coordinates)


def _major_crossing_events(graph: nx.DiGraph, node_ids: list[int]) -> int:
    count = 0
    for index in range(1, len(node_ids) - 1):
        node = node_ids[index]
        incident_major = any(_is_major(data) for _, _, data in graph.in_edges(node, data=True)) or any(
            _is_major(data) for _, _, data in graph.out_edges(node, data=True)
        )
        incoming = graph.edges[node_ids[index - 1], node]
        outgoing = graph.edges[node, node_ids[index + 1]]
        if incident_major and not _is_major(incoming) and not _is_major(outgoing):
            count += 1
    return count


def measure_route(
    graph: nx.DiGraph,
    node_ids: list[int],
    drive_distance_miles: float = 0,
) -> RouteMetrics:
    total_meters = 0.0
    trail_meters = 0.0
    repeated_meters = 0.0
    seen_edges: set[tuple[int, int]] = set()

    for u, v in zip(node_ids, node_ids[1:]):
        data = graph.edges[u, v]
        length = float(data.get("length", 0.0))
        total_meters += length
        if _is_trail(data):
            trail_meters += length
        undirected_edge = (min(u, v), max(u, v))
        if undirected_edge in seen_edges:
            repeated_meters += length
        seen_edges.add(undirected_edge)

    gain_meters = 0.0
    elevation_meters = 0.0
    for u, v in zip(node_ids, node_ids[1:]):
        start_elevation = graph.nodes[u].get("elevation")
        end_elevation = graph.nodes[v].get("elevation")
        if start_elevation is not None and end_elevation is not None:
            elevation_meters += float(graph.edges[u, v].get("length", 0.0))
            gain_meters += max(0.0, float(end_elevation) - float(start_elevation))

    signal_events = sum(1 for node in node_ids[1:] if _is_signal(graph.nodes[node]))
    return RouteMetrics(
        distance_miles=total_meters / METERS_PER_MILE,
        elevation_gain_feet=gain_meters * 3.28084,
        traffic_signal_events=signal_events,
        major_crossing_events=_major_crossing_events(graph, node_ids),
        trail_fraction=trail_meters / total_meters if total_meters else 0,
        repeated_fraction=repeated_meters / total_meters if total_meters else 0,
        drive_distance_miles=drive_distance_miles,
        elevation_coverage=elevation_meters / total_meters if total_meters else 0,
    )


def _nearest_node(graph: nx.Graph, coordinate: Coordinate) -> int:
    longitude_scale = math.cos(math.radians(coordinate.latitude))
    try:
        return int(
            min(
                graph.nodes,
                key=lambda node: (
                    (float(graph.nodes[node]["y"]) - coordinate.latitude) ** 2
                    + (
                        (float(graph.nodes[node]["x"]) - coordinate.longitude)
                        * longitude_scale
                    )
                    ** 2
                ),
            )
        )
    except ValueError as error:
        raise nx.NodeNotFound("cannot find a nearest node in an empty graph") from error


def _path(graph: nx.DiGraph, start: int, end: int) -> list[int]:
    return nx.shortest_path(graph, start, end, weight="_cost")


def _join_paths(paths: list[list[int]]) -> list[int]:
    joined: list[int] = []
    for path in paths:
        joined.extend(path if not joined else path[1:])
    return joined


def _edge_set(node_ids: list[int]) -> set[tuple[int, int]]:
    return {(min(u, v), max(u, v)) for u, v in zip(node_ids, node_ids[1:])}


def _is_distinct(edge_sets: list[set[tuple[int, int]]], candidate: set[tuple[int, int]]) -> bool:
    for existing in edge_sets:
        union = existing | candidate
        if union and len(existing & candidate) / len(union) >= 0.82:
            return False
    return True


def generate_loops(
    graph: nx.Graph,
    origin: Coordinate,
    preferences: RoutePreferences,
    drive_distance_miles: float = 0,
    elevation_loader: Callable[[nx.Graph, set[int]], None] | None = None,
) -> list[RouteCandidate]:
    """Generate triangular loop candidates across headings, widths, and radii."""

    collapsed = collapse_graph(graph, preferences)
    origin_node = _nearest_node(collapsed, origin)
    outbound_paths = nx.single_source_dijkstra_path(
        collapsed, origin_node, weight="_cost"
    )
    inbound_reversed_paths = nx.single_source_dijkstra_path(
        collapsed.reverse(copy=False), origin_node, weight="_cost"
    )
    target = preferences.target_distance_meters
    candidates: list[RouteCandidate] = []
    accepted_edge_sets: list[set[tuple[int, int]]] = []

    for scale in (0.72, 0.9, 1.08, 1.26):
        waypoint_radius = target / 3.0 * scale
        for heading in range(0, 360, 30):
            for sweep in (80, 120):
                first = destination(origin, heading - sweep / 2, waypoint_radius)
                second = destination(origin, heading + sweep / 2, waypoint_radius)
                first_node = _nearest_node(collapsed, first)
                second_node = _nearest_node(collapsed, second)
                if len({origin_node, first_node, second_node}) < 3:
                    continue
                try:
                    nodes = _join_paths(
                        [
                            outbound_paths[first_node],
                            _path(collapsed, first_node, second_node),
                            list(reversed(inbound_reversed_paths[second_node])),
                        ]
                    )
                except (KeyError, nx.NetworkXNoPath, nx.NodeNotFound):
                    continue

                metrics = measure_route(collapsed, nodes, drive_distance_miles)
                if not 0.55 * preferences.target_distance_miles <= metrics.distance_miles <= 1.5 * preferences.target_distance_miles:
                    continue
                edges = _edge_set(nodes)
                if not _is_distinct(accepted_edge_sets, edges):
                    continue
                accepted_edge_sets.append(edges)
                candidates.append(
                    RouteCandidate(
                        route_id=f"route-{len(candidates) + 1:03d}",
                        node_ids=tuple(nodes),
                        coordinates=_route_coordinates(collapsed, nodes),
                        metrics=metrics,
                    )
                )

    preliminary = rank_candidates(candidates, preferences)
    pool = preliminary[: max(20, preferences.result_count * 6)]
    if elevation_loader and pool:
        elevation_loader(
            collapsed,
            {node for candidate in pool for node in candidate.node_ids},
        )
        for candidate in pool:
            candidate.metrics = measure_route(
                collapsed, list(candidate.node_ids), drive_distance_miles
            )

    ranked = rank_candidates(pool, preferences)
    return ranked[: preferences.result_count]


def load_graph(origin: Coordinate, radius_meters: float, cache_directory: Path) -> nx.Graph:
    import osmnx as ox

    cache_directory.mkdir(parents=True, exist_ok=True)
    ox.settings.use_cache = True
    ox.settings.cache_folder = str(cache_directory / "http")
    digest = hashlib.sha256(
        f"{origin.latitude:.6f},{origin.longitude:.6f},{round(radius_meters)}".encode()
    ).hexdigest()[:16]
    graph_path = cache_directory / f"walk-{digest}.graphml"
    if graph_path.exists():
        return ox.load_graphml(graph_path)

    graph = ox.graph.graph_from_point(
        (origin.latitude, origin.longitude),
        dist=radius_meters,
        network_type="walk",
        simplify=True,
        retain_all=True,
    )
    ox.save_graphml(graph, graph_path)
    return graph


def graph_radius_meters(preferences: RoutePreferences) -> float:
    return max(2_000.0, preferences.target_distance_meters * 0.55)
