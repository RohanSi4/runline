"""Serve pool hits on a compact tile's arrays, without building its networkx graph.

A pool hit needs very little of a tile graph: the nearest node to the runner,
the streets within MAX_CONNECTOR_METERS each way of it, and the edges its
candidate loops use plus the major roads at their nodes. ``ArrayTile`` holds
a compact file's arrays (runline.compact), about a twentieth of the memory of
the networkx graph, and answers exactly those questions with the same
results the graph would give:

- ``graph`` carries the nearest-node arrays osm._nearest_node reads, in the
  graph's node order, so the same node wins every tie;
- ``reach`` is networkx's ``single_source_dijkstra`` (heap, counter, cutoff,
  minimum over parallel edges) run over the successor and predecessor
  arrays in the graph's own neighbour order, so ties break the same way;
- ``route_graph`` builds the small DiGraph serving measures, from the same
  edges in the same order serving._route_graph takes from the full graph.

Live planning still needs the full graph; runline.metros builds it from the
same arrays when a request falls back. docs/compact-tiles.md has the
measurements.
"""

from __future__ import annotations

from heapq import heappop, heappush
from itertools import count, islice
from typing import Any

import networkx as nx
import numpy as np

from .compact import COORDINATE_SCALE, _decode_coordinates, _read_file


class ArrayTile:
    """A compact tile graph's arrays, with the lookups a pool hit makes."""

    def __init__(self, header: dict, arrays: dict[str, np.ndarray]) -> None:
        from .osm import NODE_INDEX_KEY

        self.header = header
        self.arrays = arrays
        encodings = header["encodings"]
        self._vocabularies = header["vocabularies"]
        self._node_tags = header["node_tags"]
        self._node_floats = header["node_floats"]
        self._edge_tags = header["edge_tags"]
        self._edge_floats = header["edge_floats"]
        self.ids = arrays["node_ids"]
        self.x = _decode_coordinates("node_x", arrays, encodings)
        self.y = _decode_coordinates("node_y", arrays, encodings)
        self._order = np.argsort(self.ids, kind="stable")
        self._sorted = self.ids[self._order]
        # The memo osm._nearest_node builds on a graph, built here up front.
        self.graph: dict[str, Any] = {
            NODE_INDEX_KEY: (int(self.ids.size), self.ids, self.y, self.x),
            **header["graph"],
        }
        self._succ_offsets = arrays["succ_offsets"]
        self._targets = arrays["edge_target"]
        self._pred_offsets = arrays["pred_offsets"]
        self._sources = arrays["pred_sources"]
        # osm._is_major reads only ``highway``, so it is decided once per
        # distinct value and looked up per edge.
        from .osm import _is_major

        highways = self._vocabularies["edge.highway"]
        flags = np.array([False] + [_is_major({"highway": value}) for value in highways[1:]], dtype=bool)
        self._major = flags[arrays["edge_tag_highway"]]
        self._forward, self._backward = self._search_arrays()
        geometry_rows = np.flatnonzero(arrays["edge_has_geometry"])
        self._geometry_row = np.full(self._targets.size, -1, dtype=np.int64)
        self._geometry_row[geometry_rows] = np.arange(geometry_rows.size)
        self._line_x = _line_coordinates("geometry_x", arrays, encodings)
        self._line_y = _line_coordinates("geometry_y", arrays, encodings)
        # serving._route_graph's memo of major-road pairs at a node.
        self.major_at: dict[int, tuple] = {}

    @classmethod
    def read(cls, path) -> "ArrayTile":
        return cls(*_read_file(path))

    def number_of_nodes(self) -> int:
        return int(self.ids.size)

    def nbytes(self) -> int:
        held = [*self.arrays.values(), self.x, self.y, self._order, self._sorted, self._geometry_row, self._major,
                self._line_x, self._line_y, *self._forward, *self._backward, self._pair_edges, self._pair_major,
                self._pred_pair]
        # Some search arrays are the file's own; count each buffer once.
        return sum({id(array): array.nbytes for array in held}.values())

    # --- ids and data -----------------------------------------------------------

    def index(self, node: int) -> int:
        position = int(np.searchsorted(self._sorted, node))
        if position >= self._sorted.size or int(self._sorted[position]) != node:
            raise KeyError(node)
        return int(self._order[position])

    def __contains__(self, node: int) -> bool:
        try:
            self.index(node)
        except KeyError:
            return False
        return True

    def node_data(self, position: int) -> dict:
        """The node's attributes, as runline.compact.graph_from_arrays builds them."""

        data: dict[str, Any] = {"y": float(self.y[position]), "x": float(self.x[position])}
        for name in self._node_tags:
            code = int(self.arrays[f"node_tag_{name}"][position])
            if code:
                data[name] = _copy(self._vocabularies[f"node.{name}"][code])
        for name in self._node_floats:
            if self.arrays[f"node_{name}_present"][position]:
                data[name] = float(self.arrays[f"node_{name}"][position])
        return data

    def edge_data(self, edge: int, geometry: bool = True) -> dict:
        """One edge's attributes, as runline.compact.graph_from_arrays builds them.

        ``geometry=False`` leaves the line out, for a cost or a major-road
        check that does not read it; ``add_geometry`` puts it back.
        """

        data: dict[str, Any] = {}
        for name in self._edge_tags:
            code = int(self.arrays[f"edge_tag_{name}"][edge])
            if code:
                data[name] = _copy(self._vocabularies[f"edge.{name}"][code])
        for name in self._edge_floats:
            if self.arrays[f"edge_{name}_present"][edge]:
                data[name] = float(self.arrays[f"edge_{name}"][edge])
        if geometry:
            self.add_geometry(data, edge)
        return data

    def add_geometry(self, data: dict, edge: int) -> dict:
        import shapely

        row = int(self._geometry_row[edge])
        if row >= 0:
            line = int(self.arrays["geometry_line"][row])
            start, end = (int(value) for value in self.arrays["line_offsets"][line : line + 2])
            points = np.column_stack((_degrees(self._line_x[start:end]), _degrees(self._line_y[start:end])))
            if self.arrays["geometry_reversed"][row]:
                points = points[::-1]
            data["geometry"] = shapely.linestrings(points)
        return data

    def edges_between(self, u: int, v: int) -> list[int]:
        """Edge rows from u to v by position, in key order (a successor's keys are contiguous)."""

        offsets, targets, _ = self._forward
        start = int(offsets[u])
        try:
            pair = start + targets[start : int(offsets[u + 1])].tolist().index(v)
        except ValueError:
            return []
        return list(range(int(self._pair_edges[pair]), int(self._pair_edges[pair + 1])))

    def major_pairs(self, node: int) -> tuple[tuple[int, int], ...]:
        """serving._route_graph's major-road pairs at a node, in the same order.

        That is ``{*in_edges(node), *out_edges(node)}`` kept where any
        parallel edge is major. The set is built from the same tuples in the
        same order (repeats change nothing), so it iterates the same way.
        """

        position = self.index(node)
        ids = self.ids
        start, end = int(self._pred_offsets[position]), int(self._pred_offsets[position + 1])
        incoming = [(int(ids[u]), node) for u in self._sources[start:end].tolist()]
        major = dict(zip(incoming, self._pair_major[self._pred_pair[start:end]].tolist()))
        offsets, targets, _ = self._forward
        start, end = int(offsets[position]), int(offsets[position + 1])
        outgoing = [(node, int(ids[v])) for v in targets[start:end].tolist()]
        major.update(zip(outgoing, self._pair_major[start:end].tolist()))
        return tuple(pair for pair in {*incoming, *outgoing} if major[pair])

    # --- the connector search ---------------------------------------------------

    def _search_arrays(self):
        """Per node, its distinct neighbours in order with the shortest parallel length.

        The weight networkx's Dijkstra gives a multigraph edge: the minimum of
        ``length`` over the parallel edges, 1 where an edge has none.
        """

        nodes = self.ids.size
        edges = self._targets.size
        lengths = np.where(
            self.arrays["edge_length_present"].astype(bool), self.arrays["edge_length"], 1.0
        )
        owner = np.repeat(np.arange(nodes), np.diff(self._succ_offsets))
        first = np.ones(edges, dtype=bool)
        if edges:
            first[1:] = (self._targets[1:] != self._targets[:-1]) | (owner[1:] != owner[:-1])
        starts = np.flatnonzero(first)
        pair_owner = owner[starts]
        pair_target = self._targets[starts]
        pair_length = np.minimum.reduceat(lengths, starts) if edges else np.zeros(0)
        forward_offsets = np.searchsorted(pair_owner, np.arange(nodes + 1), side="left")

        # The reversed graph's neighbours are the predecessors, in their order.
        keys = pair_owner.astype(np.int64) * nodes + pair_target.astype(np.int64)
        by_key = np.argsort(keys, kind="stable")
        incoming = np.repeat(np.arange(nodes), np.diff(self._pred_offsets))
        wanted = self._sources.astype(np.int64) * nodes + incoming
        found = by_key[np.searchsorted(keys[by_key], wanted)]
        # Pair k's edges are rows _pair_edges[k] to _pair_edges[k + 1].
        self._pair_edges = np.append(starts, edges)
        self._pair_major = np.logical_or.reduceat(self._major, starts) if edges else np.zeros(0, dtype=bool)
        self._pred_pair = found
        return (
            (forward_offsets, pair_target, pair_length),
            (self._pred_offsets, self._sources, pair_length[found]),
        )

    def _dijkstra(self, source: int, cutoff: float, reverse: bool) -> tuple[dict, dict]:
        offsets, neighbours, weights = self._backward if reverse else self._forward
        dist: dict[int, Any] = {}
        seen: dict[int, Any] = {source: 0}
        previous: dict[int, int] = {}
        tie = count()
        fringe: list = [(0, next(tie), source)]
        while fringe:
            dist_v, _, v = heappop(fringe)
            if v in dist:
                continue
            dist[v] = dist_v
            start, end = int(offsets[v]), int(offsets[v + 1])
            for u, cost in zip(neighbours[start:end].tolist(), weights[start:end].tolist()):
                vu_dist = dist_v + cost
                if vu_dist > cutoff:
                    continue
                if u in dist:
                    continue
                if u not in seen or vu_dist < seen[u]:
                    seen[u] = vu_dist
                    heappush(fringe, (vu_dist, next(tie), u))
                    previous[u] = v
        paths = {source: [source]}
        for v in islice(dist, 1, None):
            paths[v] = paths[previous[v]] + [v]
        ids = self.ids
        return (
            {int(ids[v]): meters for v, meters in dist.items()},
            {int(ids[v]): [int(ids[node]) for node in path] for v, path in paths.items()},
        )

    def reach(self, node: int, cutoff: float) -> tuple[dict, dict, dict, dict]:
        """``nx.single_source_dijkstra`` by length from ``node`` both ways, within ``cutoff``."""

        position = self.index(node)
        out_meters, out_paths = self._dijkstra(position, cutoff, reverse=False)
        back_meters, back_paths = self._dijkstra(position, cutoff, reverse=True)
        return out_meters, out_paths, back_meters, back_paths

    # --- the route graph ----------------------------------------------------------

    def route_graph(self, routes: list[tuple[int, ...]], edge_cost) -> nx.DiGraph:
        """serving._route_graph on the arrays: the same edges, data and order.

        ``edge_cost(data, destination, source)`` is the runner's cost of an
        edge, osm._edge_preference_cost with the request's preferences.
        """

        small = nx.DiGraph()
        node_cache: dict[int, dict] = {}
        positions: dict[int, int] = {}

        def index(node: int) -> int:
            position = positions.get(node)
            if position is None:
                position = positions[node] = self.index(node)
            return position

        def node_data(position: int) -> dict:
            data = node_cache.get(position)
            if data is None:
                data = node_cache[position] = self.node_data(position)
            return data

        def add(node_u: int, node_v: int) -> None:
            if small.has_edge(node_u, node_v):
                return
            u, v = index(node_u), index(node_v)
            edges = self.edges_between(u, v)
            if not edges:
                raise KeyError(node_v)
            # The cost reads no geometry, so only the chosen edge gets its line.
            parallel = [self.edge_data(edge, geometry=False) for edge in edges]
            best = min(
                range(len(edges)), key=lambda position: edge_cost(parallel[position], node_data(v), node_data(u))
            )
            small.add_edge(node_u, node_v, **self.add_geometry(parallel[best], edges[best]))
            small.nodes[node_u].update(node_data(u))
            small.nodes[node_v].update(node_data(v))

        # Node ids, sets and insertion order exactly as serving._route_graph
        # has them, so the small graph comes out in the same order.
        nodes = set()
        for route in routes:
            for u, v in zip(route, route[1:]):
                add(u, v)
            nodes.update(route)
        for node in nodes:
            pairs = self.major_at.get(node)
            if pairs is None:
                pairs = self.major_at[node] = self.major_pairs(node)
            for u, v in pairs:
                add(u, v)
        return small


def _copy(value: Any) -> Any:
    return list(value) if isinstance(value, list) else value


def _line_coordinates(name: str, arrays: dict, encodings: dict) -> np.ndarray:
    """Geometry coordinates by row: whole fixed-point integers (half the
    memory of float64), or float64 as stored."""

    if encodings[name] == "delta-fixed":
        return np.cumsum(arrays[name], dtype=np.int64).astype(np.int32)
    return _decode_coordinates(name, arrays, encodings)


def _degrees(values: np.ndarray) -> np.ndarray:
    # The same division runline.compact._decode_coordinates makes.
    return values / COORDINATE_SCALE if values.dtype.kind == "i" else values
