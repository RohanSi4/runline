from __future__ import annotations

import hashlib
import math
from collections import OrderedDict
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Callable

import networkx as nx

from .coverage import (
    CoverageError,
    containing_area,
    downloads_allowed,
    find_area,
    load_area_graph,
    unsupported_message,
)
from .geo import EARTH_RADIUS_METERS, destination, distance_meters
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
    major_neighbours: dict[int, set[int]] = {}
    for u, v, _, data in _iter_edges(graph):
        if _is_major(data):
            major_neighbours.setdefault(u, set()).add(v)
            major_neighbours.setdefault(v, set()).add(u)
    crossing_nodes = {
        node for node, neighbours in major_neighbours.items() if len(neighbours) >= 2
    }
    for u, v, _, raw_data in _iter_edges(graph):
        data = dict(raw_data)
        data["_cost"] = _edge_preference_cost(data, graph.nodes[v], preferences)
        if v in crossing_nodes and not _is_major(data):
            data["_cost"] += 300.0
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
    travelled = 0.0
    last_crossing: tuple[int, float, set[str]] | None = None
    for index in range(1, len(node_ids) - 1):
        travelled += float(graph.edges[node_ids[index - 1], node_ids[index]].get("length", 0))
        node = node_ids[index]
        incoming = graph.edges[node_ids[index - 1], node]
        outgoing = graph.edges[node, node_ids[index + 1]]
        if not _is_major(incoming) and not _is_major(outgoing):
            major_edges = [
                data
                for _, _, data in graph.in_edges(node, data=True)
                if _is_major(data)
            ] + [
                data
                for _, _, data in graph.out_edges(node, data=True)
                if _is_major(data)
            ]
            if not major_edges:
                continue
            road_names = set().union(
                *(_values(data.get("name")) | _values(data.get("ref")) for data in major_edges)
            )
            if (
                last_crossing is not None
                and travelled - last_crossing[1] <= 60
                and road_names & last_crossing[2]
            ):
                prior = graph.nodes[last_crossing[0]]
                current = graph.nodes[node]
                if distance_meters(
                    Coordinate(prior["y"], prior["x"]),
                    Coordinate(current["y"], current["x"]),
                ) <= 30:
                    continue
            count += 1
            last_crossing = (node, travelled, road_names)
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


NODE_INDEX_KEY = "_runline_node_index"
PREPARED_KEY = "_runline_prepared"

# Crop/collapse/strongly-connected-core is identical for any start point in the
# same neighbourhood, and Fluid Compute reuses warm instances across requests,
# so the result is worth holding onto. Keyed by a coarse origin so that nearby
# starts share an entry; a margin covers the rounding.
PREPARED_CACHE_SIZE = 4
_PREPARED_CACHE: "OrderedDict[tuple, nx.DiGraph]" = OrderedDict()
ORIGIN_ROUNDING = 3
ORIGIN_ROUNDING_MARGIN_METERS = 250.0


def prepare_core(
    origin: Coordinate,
    radius_meters: float,
    cache_directory: Path,
    preferences: RoutePreferences,
) -> nx.DiGraph:
    """Return the collapsed, routable graph a request should search.

    Rounding the origin means two starts on the same block share one prepared
    graph. The crop radius is widened by the rounding error so the cached graph
    still fully contains the disc the caller asked for.
    """

    key_origin = Coordinate(
        round(origin.latitude, ORIGIN_ROUNDING), round(origin.longitude, ORIGIN_ROUNDING)
    )
    padded_radius = radius_meters + ORIGIN_ROUNDING_MARGIN_METERS
    key = (
        key_origin.latitude,
        key_origin.longitude,
        round(padded_radius),
        preferences.surface.value,
    )
    cached = _PREPARED_CACHE.get(key)
    if cached is not None:
        _PREPARED_CACHE.move_to_end(key)
        return cached

    graph = load_graph(key_origin, padded_radius, cache_directory)
    core = _routable_core(collapse_graph(graph, preferences))
    core.graph[PREPARED_KEY] = True

    _PREPARED_CACHE[key] = core
    while len(_PREPARED_CACHE) > PREPARED_CACHE_SIZE:
        _PREPARED_CACHE.popitem(last=False)
    return core


def _node_index(graph: nx.Graph) -> tuple[Any, Any, Any]:
    """Node ids and coordinates as parallel arrays, cached on the graph.

    ``generate_loops`` asks for a nearest node ~200 times per request, so a
    per-node Python scan dominates the whole planner on a city-sized graph.
    The arrays are stashed in ``graph.graph`` so they live and die with the
    graph, and are rebuilt whenever the node count no longer matches.
    """

    import numpy as np

    cached = graph.graph.get(NODE_INDEX_KEY)
    if cached is not None and cached[0] == graph.number_of_nodes():
        return cached[1], cached[2], cached[3]

    nodes: list[int] = []
    latitudes: list[float] = []
    longitudes: list[float] = []
    for node, data in graph.nodes(data=True):
        nodes.append(node)
        latitudes.append(float(data["y"]))
        longitudes.append(float(data["x"]))

    index = (
        np.asarray(nodes, dtype=np.int64),
        np.asarray(latitudes, dtype=np.float64),
        np.asarray(longitudes, dtype=np.float64),
    )
    graph.graph[NODE_INDEX_KEY] = (len(nodes), *index)
    return index


def _nearest_node(graph: nx.Graph, coordinate: Coordinate) -> int:
    import numpy as np

    node_ids, latitudes, longitudes = _node_index(graph)
    if node_ids.size == 0:
        raise nx.NodeNotFound("cannot find a nearest node in an empty graph")

    longitude_scale = math.cos(math.radians(coordinate.latitude))
    latitude_delta = latitudes - coordinate.latitude
    longitude_delta = (longitudes - coordinate.longitude) * longitude_scale
    return int(
        node_ids[
            int(np.argmin(latitude_delta * latitude_delta + longitude_delta * longitude_delta))
        ]
    )


def _path(graph: nx.DiGraph, start: int, end: int) -> list[int]:
    # The chord is a metric. Scaling it by the minimum edge cost/chord ratio
    # makes the heuristic admissible even when a graph has bad edge lengths.
    # Benchmarks favored A* below 30k nodes and bidirectional Dijkstra above it.
    if graph.number_of_nodes() < 30_000:
        heuristic = graph.graph.get("_chord_heuristic")
        if heuristic is None:
            positions = {}
            for node, data in graph.nodes(data=True):
                latitude, longitude = math.radians(data["y"]), math.radians(data["x"])
                radius = EARTH_RADIUS_METERS * math.cos(latitude)
                positions[node] = (
                    radius * math.cos(longitude),
                    radius * math.sin(longitude),
                    EARTH_RADIUS_METERS * math.sin(latitude),
                )
            scale = min(
                (
                    data["_cost"] / chord
                    for u, v, data in graph.edges(data=True)
                    if (chord := math.dist(positions[u], positions[v])) > 0
                ),
                default=0,
            )
            heuristic = (positions, scale)
            graph.graph["_chord_heuristic"] = heuristic
        positions, scale = heuristic
        return nx.astar_path(
            graph, start, end,
            heuristic=lambda u, v: scale * math.dist(positions[u], positions[v]),
            weight="_cost",
        )
    return nx.shortest_path(graph, start, end, weight="_cost")


def _join_paths(paths: list[list[int]]) -> list[int]:
    joined: list[int] = []
    for path in paths:
        joined.extend(path if not joined else path[1:])
    return joined


# At 0.82, two selected 5-mile options shared 78% of their road length.
MAX_SHARED_LENGTH_FRACTION = 0.7


def _edge_lengths(graph: nx.DiGraph, node_ids: tuple[int, ...]) -> dict[tuple[int, int], float]:
    return {
        (min(u, v), max(u, v)): float(graph.edges[u, v].get("length", 0))
        for u, v in zip(node_ids, node_ids[1:])
    }


def _distinct_by_length(
    graph: nx.DiGraph, ranked: list[RouteCandidate], limit: int
) -> list[RouteCandidate]:
    selected: list[RouteCandidate] = []
    selected_edges: list[dict[tuple[int, int], float]] = []
    for candidate in ranked:
        edges = _edge_lengths(graph, candidate.node_ids)
        duplicate = False
        for existing in selected_edges:
            shared = sum(min(length, existing.get(edge, 0)) for edge, length in edges.items())
            union = sum(edges.values()) + sum(existing.values()) - shared
            if union and shared / union >= MAX_SHARED_LENGTH_FRACTION:
                duplicate = True
                break
        if not duplicate:
            selected.append(candidate)
            selected_edges.append(edges)
            if len(selected) >= limit:
                break
    return selected


def _routable_core(graph: nx.DiGraph) -> nx.DiGraph:
    """Drop nodes that cannot both be reached from and return to the rest.

    OSM extracts are built with ``retain_all=True``, so they include isolated
    fragments such as a stranded footway or a service loop inside a car park.
    Picking a start on one of those islands makes every waypoint unreachable
    and the planner returns nothing, which is what happened for start points
    near central Richmond. Only a strongly connected component can host an
    out-and-back loop, so the planner works within the largest one.
    """

    components = list(nx.strongly_connected_components(graph))
    if not components:
        return graph
    core = max(components, key=len)
    if len(core) == graph.number_of_nodes():
        return graph

    trimmed = graph.subgraph(core).copy()
    trimmed.graph.pop(NODE_INDEX_KEY, None)
    return trimmed


def generate_loops(
    graph: nx.Graph,
    origin: Coordinate,
    preferences: RoutePreferences,
    drive_distance_miles: float = 0,
    elevation_loader: Callable[[nx.Graph, set[int]], None] | None = None,
) -> list[RouteCandidate]:
    """Generate triangular loop candidates across headings, widths, and radii."""

    if graph.graph.get(PREPARED_KEY):
        collapsed = graph
    else:
        collapsed = _routable_core(collapse_graph(graph, preferences))
    origin_node = _nearest_node(collapsed, origin)
    outbound_paths = nx.single_source_dijkstra_path(
        collapsed, origin_node, weight="_cost"
    )
    inbound_reversed_paths = nx.single_source_dijkstra_path(
        collapsed.reverse(copy=False), origin_node, weight="_cost"
    )
    target = preferences.target_distance_meters
    candidates: list[RouteCandidate] = []
    candidate_legs: dict[str, tuple[list[int], list[int], list[int]]] = {}

    def sample(scale: float, heading: int, sweep: int) -> None:
        waypoint_radius = target / 3.0 * scale
        first = destination(origin, heading - sweep / 2, waypoint_radius)
        second = destination(origin, heading + sweep / 2, waypoint_radius)
        first_node = _nearest_node(collapsed, first)
        second_node = _nearest_node(collapsed, second)
        if len({origin_node, first_node, second_node}) < 3:
            return
        try:
            legs = (
                outbound_paths[first_node],
                _path(collapsed, first_node, second_node),
                list(reversed(inbound_reversed_paths[second_node])),
            )
            nodes = _join_paths(list(legs))
        except (KeyError, nx.NetworkXNoPath, nx.NodeNotFound):
            return

        metrics = measure_route(collapsed, nodes, drive_distance_miles)
        if not 0.55 * preferences.target_distance_miles <= metrics.distance_miles <= 1.5 * preferences.target_distance_miles:
            return
        route_id = f"route-{len(candidates) + 1:03d}"
        candidate_legs[route_id] = legs
        candidates.append(RouteCandidate(route_id, tuple(nodes), (), metrics))

    scales = (
        (0.6, 0.72, 0.9, 1.08, 1.26)
        if collapsed.graph.get("coverage_limited")
        else (0.72, 0.9, 1.08, 1.26)
    )
    for scale in scales:
        for heading in range(0, 360, 30):
            for sweep in (80, 120):
                sample(scale, heading, sweep)

    feasible = [
        candidate for candidate in candidates
        if abs(candidate.metrics.distance_miles - preferences.target_distance_miles)
        <= preferences.distance_tolerance_miles
    ]
    feasible_ranked = rank_candidates(feasible, preferences)
    if len(_distinct_by_length(collapsed, feasible_ranked, preferences.result_count)) < preferences.result_count:
        for scale, sweep in ((0.6, 60), (0.6, 90), (0.7, 60), (0.8, 60)):
            for heading in range(0, 360, 30):
                sample(scale, heading, sweep)
    elif (
        feasible_ranked
        and abs(feasible_ranked[0].metrics.distance_miles - preferences.target_distance_miles)
        > 0.4 * preferences.distance_tolerance_miles
    ):
        for heading in range(0, 360, 30):
            sample(0.65, heading, 90)

    preliminary = _distinct_by_length(
        collapsed, rank_candidates(candidates, preferences), preferences.result_count
    )
    for candidate in preliminary:
        if candidate.metrics.repeated_fraction < 0.12:
            continue
        first, middle, last = candidate_legs[candidate.route_id]
        outbound = _join_paths([first, middle])
        used = {(min(u, v), max(u, v)) for u, v in zip(outbound, outbound[1:])}

        def return_cost(u: int, v: int, data: dict[str, Any]) -> float:
            return data["_cost"] * (3 if (min(u, v), max(u, v)) in used else 1)

        try:
            alternate = nx.shortest_path(collapsed, last[0], origin_node, weight=return_cost)
        except (nx.NetworkXNoPath, nx.NodeNotFound):
            continue
        if alternate == last:
            continue
        nodes = _join_paths([first, middle, alternate])
        metrics = measure_route(collapsed, nodes, drive_distance_miles)
        if 0.55 * preferences.target_distance_miles <= metrics.distance_miles <= 1.5 * preferences.target_distance_miles:
            candidates.append(RouteCandidate(f"route-{len(candidates) + 1:03d}", tuple(nodes), (), metrics))

    pool = _distinct_by_length(
        collapsed, rank_candidates(candidates, preferences), max(20, preferences.result_count * 6)
    )
    if elevation_loader and pool:
        elevation_loader(
            collapsed,
            {node for candidate in pool for node in candidate.node_ids},
        )
        for candidate in pool:
            candidate.metrics = measure_route(
                collapsed, list(candidate.node_ids), drive_distance_miles
            )

    ranked = _distinct_by_length(collapsed, rank_candidates(pool, preferences), preferences.result_count)
    for candidate in ranked:
        candidate.coordinates = _route_coordinates(collapsed, list(candidate.node_ids))
    return ranked


def crop_to_radius(
    graph: nx.MultiDiGraph, origin: Coordinate, radius_meters: float
) -> nx.MultiDiGraph:
    """Cut a city-wide graph down to the disc a single request actually needs.

    Precomputed areas span a whole city so that one artifact serves every start
    point in it, but collapsing and searching all of that per request is both
    slow and memory-hungry. A 5 mile run in Washington DC touches roughly a
    tenth of the city graph.

    A degrees-based bounding box rejects most nodes before any trigonometry.
    """

    latitude_span = radius_meters / 111_320.0
    longitude_span = latitude_span / max(
        math.cos(math.radians(origin.latitude)), 1e-6
    )
    minimum_latitude = origin.latitude - latitude_span
    maximum_latitude = origin.latitude + latitude_span
    minimum_longitude = origin.longitude - longitude_span
    maximum_longitude = origin.longitude + longitude_span

    keep = [
        node
        for node, data in graph.nodes(data=True)
        if minimum_latitude <= data["y"] <= maximum_latitude
        and minimum_longitude <= data["x"] <= maximum_longitude
        and distance_meters(origin, Coordinate(data["y"], data["x"])) <= radius_meters
    ]
    cropped = graph.subgraph(keep).copy()
    cropped.graph.pop(NODE_INDEX_KEY, None)
    return cropped


def load_graph(origin: Coordinate, radius_meters: float, cache_directory: Path) -> nx.Graph:
    """Return a walk graph around ``origin``, marking limited map coverage.

    A fully covering precomputed area is preferred. When downloads are
    disabled, an area containing the start can still serve the intersection
    of its map and the requested crop. An origin outside every area raises
    ``CoverageError``.
    """

    area = find_area(origin, radius_meters)
    if area is not None:
        return crop_to_radius(load_area_graph(area), origin, radius_meters)

    if not downloads_allowed():
        partial_area = containing_area(origin)
        if partial_area is None:
            raise CoverageError(unsupported_message(origin, radius_meters))
        graph = crop_to_radius(load_area_graph(partial_area), origin, radius_meters)
        graph.graph["coverage_limited"] = True
        return graph

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
