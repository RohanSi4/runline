from __future__ import annotations

import hashlib
import math
import threading
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
from .geo import destination, distance_meters
from .models import (
    METERS_PER_MILE,
    Coordinate,
    RouteCandidate,
    RouteMetrics,
    RoutePreferences,
    SurfacePreference,
)
from .scoring import MAX_REPEATED, MIN_COMPACTNESS, ROUND_ENOUGH, rank_candidates


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
        # Entering a node with major road on two sides usually means crossing
        # it. An edge cost cannot see the turn, so same-side turns pay too; it
        # still cut geometric crossings 38% with no loss of in-band routes.
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


def _bearing_from(graph: nx.DiGraph, u: int, v: int, node: int) -> float:
    """Direction leaving ``node`` along edge (u, v), taken ~15 m out so a
    curved approach reads the way the road actually goes."""

    points = _edge_geometry_coordinates(graph, u, v)
    if node == v:
        points.reverse()
    start = points[0]
    scale = math.cos(math.radians(start.latitude))
    for point in points[1:]:
        dx = (point.longitude - start.longitude) * scale
        dy = point.latitude - start.latitude
        if math.hypot(dx, dy) * 111_320 >= 15:
            break
    return math.atan2(dy, dx)


def _separates(bearings: list[float], first: float, second: float) -> bool:
    """True when major-road branches lie on both arcs between two directions."""

    span = (second - first) % math.tau
    inside = [(bearing - first) % math.tau < span for bearing in bearings]
    return any(inside) and not all(inside)


def _left_of(direction: float, point: float) -> bool:
    return math.sin(point - direction) > 0


MAJOR_EDGES_KEY = "_runline_major_edges"


def _major_edge_index(graph: nx.DiGraph) -> dict[int, dict[int, tuple[int, int, dict[str, Any]]]]:
    """Per-node memo of major-road edges, keyed by the neighbour, cached on the graph.

    ``_major_crossings`` needs these at every node of every candidate route,
    and rebuilding them from ``in_edges``/``out_edges`` each time was most of
    ``measure_route``. Filled lazily, so a one-off request graph only pays for
    the nodes its routes visit.
    """

    cached = graph.graph.get(MAJOR_EDGES_KEY)
    if cached is not None and cached[0] == graph.number_of_nodes():
        return cached[1]
    index: dict[int, dict[int, tuple[int, int, dict[str, Any]]]] = {}
    graph.graph[MAJOR_EDGES_KEY] = (graph.number_of_nodes(), index)
    return index


def _major_edges_at(graph: nx.DiGraph, index: dict, node: int) -> dict[int, tuple[int, int, dict[str, Any]]]:
    edges = index.get(node)
    if edges is None:
        # In-edges first, then out-edges; a later edge to the same neighbour
        # replaces an earlier one, as in the scan this replaces.
        edges = {
            u if v == node else v: (u, v, data)
            for u, v, data in (*graph.in_edges(node, data=True), *graph.out_edges(node, data=True))
            if _is_major(data)
        }
        index[node] = edges
    return edges


def _major_crossings(graph: nx.DiGraph, node_ids: list[int]) -> list[int]:
    """Route positions where the route changes side across a major road.

    At a node on a major road, the route crosses when its approach and exit lie
    between different major branches; a same-side turn or a dead end is not a
    crossing. A route that walks along a major road crosses when it leaves on
    the other side from the one it joined, and at every major branch it passes
    on its side. Crossings of branches meeting at one node within 100 m count
    once (dual-carriageway junctions).
    """

    major_index = _major_edge_index(graph)
    crossings: list[int] = []
    travelled = 0.0
    joined_left: bool | None = None
    last_crossing: tuple[int, float, set[str], set[int]] | None = None
    for index in range(1, len(node_ids) - 1):
        previous, node, following = node_ids[index - 1 : index + 2]
        incoming = graph.edges[previous, node]
        outgoing = graph.edges[node, following]
        travelled += float(incoming.get("length", 0))
        major_edges = _major_edges_at(graph, major_index, node)
        if not major_edges:
            continue
        incoming_major, outgoing_major = _is_major(incoming), _is_major(outgoing)
        back = _bearing_from(graph, previous, node, node)
        ahead = _bearing_from(graph, node, following, node)
        if not (incoming_major or outgoing_major):
            branches = [_bearing_from(graph, u, v, node) for u, v, _ in major_edges.values()]
            crossed = _separates(branches, back, ahead)
        else:
            if outgoing_major and not incoming_major:
                joined_left = _left_of(ahead, back)
            # Walking along a major road on one side, the route crosses any
            # other major branch it passes on that side, and the road itself
            # when it leaves on the far side.
            others = [
                _bearing_from(graph, u, v, node)
                for other, (u, v, _) in major_edges.items()
                if other not in (previous, following)
            ]
            if joined_left is None:
                crossed = False
            elif joined_left:
                crossed = any((b - ahead) % math.tau < (back - ahead) % math.tau for b in others)
            else:
                crossed = any((b - back) % math.tau < (ahead - back) % math.tau for b in others)
            if not outgoing_major:
                crossed = crossed or (
                    joined_left is not None and joined_left != _left_of(back + math.pi, ahead)
                )
                joined_left = None
        if not crossed:
            continue
        road_names = set().union(
            *(_values(data.get("name")) | _values(data.get("ref")) for _, _, data in major_edges.values())
        )
        if (
            last_crossing is not None
            and travelled - last_crossing[1] <= 100
            and set(major_edges) & last_crossing[3]
        ):
            continue
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
        crossings.append(index)
        last_crossing = (node, travelled, road_names, set(major_edges))
    return crossings


def _compactness(graph: nx.DiGraph, node_ids: list[int]) -> float:
    """Isoperimetric ratio of the route's outline, in local metres."""

    if len(node_ids) < 4:
        return 0.0
    latitude = float(graph.nodes[node_ids[0]]["y"])
    x_scale = 111_320.0 * math.cos(math.radians(latitude))
    points = [
        (float(graph.nodes[node]["x"]) * x_scale, float(graph.nodes[node]["y"]) * 110_540.0)
        for node in node_ids
    ]
    area = 0.0
    perimeter = 0.0
    for (x1, y1), (x2, y2) in zip(points, points[1:] + points[:1]):
        area += x1 * y2 - x2 * y1
        perimeter += math.hypot(x2 - x1, y2 - y1)
    if perimeter == 0:
        return 0.0
    return min(1.0, 4 * math.pi * abs(area) / 2 / perimeter**2)


# Out on a street and back on its sidewalk is two edges, so counting repeated
# edges misses it. Within 20 m, heading the other way, it is the same street.
# The gap keeps a sample from matching its own neighbours at a U-turn.
DOUBLED_BACK_METERS = 20.0
DOUBLED_BACK_GAP_METERS = 2 * DOUBLED_BACK_METERS
DOUBLED_BACK_STEP_METERS = 10.0


def doubled_back_fraction(coordinates: tuple[Coordinate, ...] | list[Coordinate]) -> float:
    """Share of a route's length that runs back along an earlier part of it.

    The route is sampled every 10 m; a sample counts when an earlier sample at
    least 40 m back along the route lies within 20 m and heads the opposite
    way. Only the later pass counts, as with repeated edges, so a pure out and
    back is 0.5. A lap of a block heads the same way, so it does not count.
    """

    import numpy as np

    if len(coordinates) < 2:
        return 0.0
    x_scale = 111_320.0 * math.cos(math.radians(coordinates[0].latitude))
    return _doubled_back(np.array([(c.longitude * x_scale, c.latitude * 110_540.0) for c in coordinates]))


EDGE_POINTS_KEY = "_runline_edge_points"


def _route_points(graph: nx.DiGraph, node_ids: list[int]):
    """The route's drawn line in local metres, from a per-edge memo on the graph.

    Building ``_route_coordinates`` for every candidate cost more than the
    rest of ``measure_route``; a request's candidates share most of their
    edges, so each edge's line is projected once.
    """

    import numpy as np

    cached = graph.graph.get(EDGE_POINTS_KEY)
    if cached is None or cached[0] != graph.number_of_nodes():
        x_scale = 111_320.0 * math.cos(math.radians(float(graph.nodes[node_ids[0]]["y"])))
        cached = (graph.number_of_nodes(), x_scale, {})
        graph.graph[EDGE_POINTS_KEY] = cached
    _, x_scale, index = cached
    parts = []
    for u, v in zip(node_ids, node_ids[1:]):
        points = index.get((u, v))
        if points is None:
            points = np.array(
                [(c.longitude * x_scale, c.latitude * 110_540.0) for c in _edge_geometry_coordinates(graph, u, v)]
            )
            index[(u, v)] = points
        parts.append(points)
    return np.concatenate(parts)


def _doubled_back(points) -> float:
    """``doubled_back_fraction`` on a line already in local metres."""

    import numpy as np
    from scipy.spatial import cKDTree

    segments = np.diff(points, axis=0)
    lengths = np.hypot(segments[:, 0], segments[:, 1])
    keep = lengths > 0
    starts, segments, lengths = points[:-1][keep], segments[keep], lengths[keep]
    total = float(lengths.sum())
    if total <= 2 * DOUBLED_BACK_GAP_METERS:
        return 0.0
    counts = np.maximum(1, (lengths // DOUBLED_BACK_STEP_METERS).astype(int))
    segment = np.repeat(np.arange(len(lengths)), counts)
    offsets = np.arange(len(segment)) - np.repeat(np.cumsum(counts) - counts, counts)
    share = offsets / counts[segment]
    samples = starts[segment] + segments[segment] * share[:, None]
    headings = segments[segment] / lengths[segment][:, None]
    along = (np.cumsum(lengths) - lengths)[segment] + lengths[segment] * share

    pairs = cKDTree(samples).query_pairs(DOUBLED_BACK_METERS, output_type="ndarray")
    if not len(pairs):
        return 0.0
    first, second = pairs[:, 0], pairs[:, 1]
    gap = np.abs(along[first] - along[second])
    # A loop ends where it starts, so the two ends are close along the route.
    gap = np.minimum(gap, total - gap)
    opposite = np.einsum("ij,ij->i", headings[first], headings[second]) < -0.5
    matched = (gap > DOUBLED_BACK_GAP_METERS) & opposite
    later = np.where(along[first] > along[second], first, second)
    doubled = np.zeros(len(samples), dtype=bool)
    doubled[later[matched]] = True
    weights = lengths[segment] / counts[segment]
    return float(weights[doubled].sum() / total)


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
    edge_repeats = repeated_meters / total_meters if total_meters else 0
    return RouteMetrics(
        distance_miles=total_meters / METERS_PER_MILE,
        elevation_gain_feet=gain_meters * 3.28084,
        traffic_signal_events=signal_events,
        major_crossing_events=len(_major_crossings(graph, node_ids)),
        trail_fraction=trail_meters / total_meters if total_meters else 0,
        # Repeated edges catch a lap run twice; geometry catches parallel paths.
        repeated_fraction=max(edge_repeats, _doubled_back(_route_points(graph, node_ids)) if len(node_ids) > 1 else 0),
        drive_distance_miles=drive_distance_miles,
        elevation_coverage=elevation_meters / total_meters if total_meters else 0,
        compactness=_compactness(graph, node_ids),
    )


NODE_INDEX_KEY = "_runline_node_index"
PREPARED_KEY = "_runline_prepared"

# Crop/collapse/strongly-connected-core is identical for any start point in the
# same neighbourhood, and Fluid Compute reuses warm instances across requests,
# so the result is worth holding onto. Keyed by a coarse origin so that nearby
# starts share an entry; a margin covers the rounding.
PREPARED_CACHE_SIZE = 4
_PREPARED_CACHE: "OrderedDict[tuple, nx.DiGraph]" = OrderedDict()
_PREPARED_CACHE_LOCK = threading.Lock()
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
    with _PREPARED_CACHE_LOCK:
        cached = _PREPARED_CACHE.get(key)
        if cached is not None:
            _PREPARED_CACHE.move_to_end(key)
            return cached

    graph = load_graph(key_origin, padded_radius, cache_directory)
    core = _routable_core(collapse_graph(graph, preferences))
    core.graph[PREPARED_KEY] = True

    with _PREPARED_CACHE_LOCK:
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


COST_MATRIX_KEY = "_runline_cost_matrix"


class _CostMatrix:
    """The graph's ``_cost`` weights as a sparse matrix for scipy's Dijkstra.

    NetworkX runs Dijkstra in Python, and point-to-point searches were 75% of
    an 8 mile request (3.3 of 4.4s). scipy's compiled single-source search is
    about 5x faster per call on a 32k-node graph, even though it does not stop
    at the target.
    """

    def __init__(self, graph: nx.DiGraph) -> None:
        import numpy as np
        from scipy.sparse import csr_matrix

        self.nodes = list(graph.nodes)
        self.index = {node: position for position, node in enumerate(self.nodes)}
        rows, columns, costs = [], [], []
        for u, v, data in graph.edges(data=True):
            rows.append(self.index[u])
            columns.append(self.index[v])
            # csgraph treats an explicit zero as a missing edge.
            costs.append(max(float(data["_cost"]), 1e-9))
        size = len(self.nodes)
        self.forward = csr_matrix(
            (np.asarray(costs), (np.asarray(rows), np.asarray(columns))), shape=(size, size)
        )
        self.forward.sort_indices()
        self.backward = self.forward.T.tocsr()

    def _position(self, matrix, u: int, v: int) -> int:
        row = self.index[u]
        start, end = matrix.indptr[row], matrix.indptr[row + 1]
        return start + int((matrix.indices[start:end] == self.index[v]).nonzero()[0][0])

    def tree(
        self, source: int, *, reverse: bool = False, matrix=None, limit: float | None = None
    ) -> "_PathTree":
        """Shortest paths from ``source``, searching no further than ``limit``.

        A bounded search finalises nodes in the same order as a full one, so
        every node it reaches gets the same path. A node beyond the bound is
        not missing, just unsearched: the tree then reruns without the bound
        on first need, so the result never depends on the limit chosen, only
        the time does.
        """

        if source not in self.index:
            raise nx.NodeNotFound(f"node {source} is not in the graph")
        graph = matrix if matrix is not None else (self.backward if reverse else self.forward)
        return _PathTree(self, self.index[source], graph, limit)

    def penalized(self, edges: set[tuple[int, int]], factor: float):
        """A copy of the forward matrix with ``edges`` (either direction) scaled."""

        matrix = self.forward.copy()
        for u, v in edges:
            for a, b in ((u, v), (v, u)):
                if a in self.index and b in self.index and self.forward[self.index[a], self.index[b]]:
                    matrix.data[self._position(matrix, a, b)] *= factor
        return matrix


class _PathTree:
    """Shortest paths from one source, read out per target like a dict."""

    def __init__(self, costs: _CostMatrix, source: int, matrix, limit: float | None) -> None:
        self._costs = costs
        self._source = source
        self._matrix = matrix
        self._limit = limit
        self._predecessors = self._search(limit)

    def _search(self, limit: float | None):
        from scipy.sparse.csgraph import dijkstra

        _, predecessors = dijkstra(
            self._matrix,
            indices=self._source,
            return_predecessors=True,
            limit=float("inf") if limit is None else limit,
        )
        return predecessors

    def __getitem__(self, target: int) -> list[int]:
        index = self._costs.index.get(target)
        if index is None:
            raise KeyError(target)
        if index != self._source and self._predecessors[index] < 0 and self._limit is not None:
            # Beyond the bound, or unreachable: only a full search can tell.
            self._limit = None
            self._predecessors = self._search(None)
        path = [index]
        while index != self._source:
            index = int(self._predecessors[index])
            if index < 0:
                raise KeyError(target)
            path.append(index)
        nodes = self._costs.nodes
        return [nodes[position] for position in reversed(path)]


def _cost_matrix(graph: nx.DiGraph) -> _CostMatrix:
    cached = graph.graph.get(COST_MATRIX_KEY)
    if cached is not None and len(cached.nodes) == graph.number_of_nodes():
        return cached
    costs = _CostMatrix(graph)
    graph.graph[COST_MATRIX_KEY] = costs
    return costs


def _path(graph: nx.DiGraph, start: int, end: int, limit: float | None = None) -> list[int]:
    # A* with a chord heuristic matched these costs but was slower overall:
    # 0.6x at the Rotunda for 3 miles, 1.1-2x at other origins and distances.
    try:
        return _cost_matrix(graph).tree(start, limit=limit)[end]
    except KeyError:
        raise nx.NetworkXNoPath(f"no path from {start} to {end}") from None


def _join_paths(paths: list[list[int]]) -> list[int]:
    joined: list[int] = []
    for path in paths:
        joined.extend(path if not joined else path[1:])
    return joined


def _trim_spurs(node_ids: list[int]) -> list[int]:
    """Drop out-and-back detours, so A-B-C-B-A becomes A.

    A waypoint that snaps onto a dead end makes the route run up the street and
    straight back out. Each reversal is undone as soon as it appears, so nested
    spurs unwind fully. Only exact reversals go: a lap of a block stays.
    """

    trimmed: list[int] = []
    for node in node_ids:
        if len(trimmed) >= 2 and trimmed[-2] == node:
            trimmed.pop()
        else:
            trimmed.append(node)
    return trimmed


def _separated_return(
    graph: nx.DiGraph, outbound: list[int], start: int, end: int, limit: float | None = None
) -> list[int]:
    """Shortest way home that avoids the streets the outbound legs used.

    The cheapest return is usually the quiet street the route went out on, so
    plain loops come back on themselves or on the next street over. Tripling
    the cost of used edges keeps them available but pushes the return away.
    """

    costs = _cost_matrix(graph)
    used = set(zip(outbound, outbound[1:]))
    try:
        return costs.tree(start, matrix=costs.penalized(used, 3), limit=limit)[end]
    except KeyError:
        raise nx.NetworkXNoPath(f"no path from {start} to {end}") from None


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
    trimmed.graph.pop(COST_MATRIX_KEY, None)
    trimmed.graph.pop(MAJOR_EDGES_KEY, None)
    trimmed.graph.pop(EDGE_POINTS_KEY, None)
    return trimmed


# Search bound in cost units (metres plus penalties) per metre of target.
SEARCH_LIMIT_FACTOR = 2.0


def generate_loops(
    graph: nx.Graph,
    origin: Coordinate,
    preferences: RoutePreferences,
    drive_distance_miles: float = 0,
    elevation_loader: Callable[[nx.Graph, set[int]], None] | None = None,
    pool_size: int | None = None,
) -> list[RouteCandidate]:
    """Generate triangular loop candidates across headings, widths, and radii.

    With ``pool_size``, return up to that many distinct candidates, ranked but
    without coordinates, instead of the final ``result_count`` routes. The
    route-pool precompute uses this to keep loops of every length it finds.
    """

    if graph.graph.get(PREPARED_KEY):
        collapsed = graph
    else:
        collapsed = _routable_core(collapse_graph(graph, preferences))
    origin_node = _nearest_node(collapsed, origin)
    costs = _cost_matrix(collapsed)
    target = preferences.target_distance_meters
    # No leg of a loop the planner keeps can cost much more than the loop is
    # long, so searches stop there instead of covering the whole graph. The
    # bound only saves time: a leg past it is found by a full search.
    search_limit = target * SEARCH_LIMIT_FACTOR
    outbound_paths = costs.tree(origin_node, limit=search_limit)
    inbound_reversed_paths = costs.tree(origin_node, reverse=True, limit=search_limit)
    target_miles = preferences.target_distance_miles
    candidates: list[RouteCandidate] = []
    candidate_legs: dict[str, tuple[list[int], list[int], list[int]]] = {}
    # (heading, sweep) -> (scale, miles) of the attempt closest to the target.
    closest: dict[tuple[int, int], tuple[float, float]] = {}

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
                _path(collapsed, first_node, second_node, search_limit),
                list(reversed(inbound_reversed_paths[second_node])),
            )
            nodes = _trim_spurs(_join_paths(list(legs)))
        except (KeyError, nx.NetworkXNoPath, nx.NodeNotFound):
            return
        if len(nodes) < 4:
            # Nothing left but an out-and-back once the spurs are gone.
            return

        metrics = measure_route(collapsed, nodes, drive_distance_miles)
        known = closest.get((heading, sweep))
        if known is None or abs(metrics.distance_miles - target_miles) < abs(known[1] - target_miles):
            closest[heading, sweep] = (scale, metrics.distance_miles)
        if not 0.55 * target_miles <= metrics.distance_miles <= 1.5 * target_miles:
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

    # Slivers do not count towards a full set, so a direction that only
    # produced thin loops gets the extra sampling below.
    feasible = [
        candidate for candidate in candidates
        if abs(candidate.metrics.distance_miles - preferences.target_distance_miles)
        <= preferences.distance_tolerance_miles
        and candidate.metrics.compactness >= MIN_COMPACTNESS
    ]
    feasible_ranked = rank_candidates(feasible, preferences)
    if len(_distinct_by_length(collapsed, feasible_ranked, preferences.result_count)) < preferences.result_count:
        # Route length grows roughly in proportion to waypoint radius, so
        # rescale each direction's closest attempt onto the target.
        for (heading, sweep), (scale, miles) in list(closest.items()):
            if abs(miles - target_miles) > preferences.distance_tolerance_miles:
                sample(scale * target_miles / miles, heading, sweep)
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

    # Re-route the return of shortlisted loops that come back on themselves or
    # run out and back on neighbouring streets. Doing this per sample instead
    # cost up to 40% more time at 12 miles for the same shapes.
    shortlist = _distinct_by_length(
        collapsed, rank_candidates(candidates, preferences), preferences.result_count * 2
    )
    for candidate in shortlist:
        if (
            candidate.metrics.repeated_fraction < 0.12
            and candidate.metrics.compactness >= ROUND_ENOUGH
        ):
            continue
        first, middle, last = candidate_legs[candidate.route_id]
        try:
            alternate = _separated_return(
                collapsed, _join_paths([first, middle]), last[0], origin_node, search_limit
            )
        except (nx.NetworkXNoPath, nx.NodeNotFound):
            continue
        if alternate == last:
            continue
        nodes = _trim_spurs(_join_paths([first, middle, alternate]))
        if len(nodes) < 4:
            continue
        metrics = measure_route(collapsed, nodes, drive_distance_miles)
        if 0.55 * preferences.target_distance_miles <= metrics.distance_miles <= 1.5 * preferences.target_distance_miles:
            candidates.append(RouteCandidate(f"route-{len(candidates) + 1:03d}", tuple(nodes), (), metrics))

    if pool_size is not None:
        # Serving never answers with a loop that doubles back (see
        # serving.MAX_DOUBLED_BACK), so such loops would only take the slots
        # of ones it can serve. Live ranking keeps them as a last resort.
        candidates = [candidate for candidate in candidates if candidate.metrics.repeated_fraction <= MAX_REPEATED]
    pool = _distinct_by_length(
        collapsed,
        rank_candidates(candidates, preferences),
        pool_size or max(20, preferences.result_count * 6),
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
    if pool_size is not None:
        return pool

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
    cropped.graph.pop(COST_MATRIX_KEY, None)
    cropped.graph.pop(MAJOR_EDGES_KEY, None)
    cropped.graph.pop(EDGE_POINTS_KEY, None)
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
