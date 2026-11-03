"""Answer route requests from precomputed pools instead of searching live.

A request's start point names a grid cell; that cell's pool (built by
scripts/precompute_pools.py, merged by scripts/merge_pools.py) already holds
loops at every whole-mile target. Serving keeps the ones near the requested
distance, ranks them with the same scoring live routes use, and draws the
winners.

Pools are an optimization, never a requirement. ``serve_from_pool`` returns
None whenever it cannot give a full answer (no file, an old format, a pool
built from a different graph, a cell with no pool, too few loops in the
distance band, a drive-radius request) and the planner searches live.
"""

from __future__ import annotations

import functools
import hashlib
import json
import logging
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import networkx as nx

from .coverage import Area, containing_area, graphs_root, load_area_graph
from .grid import cell_id
from .models import METERS_PER_MILE, Coordinate, RouteCandidate, RouteMetrics, RoutePreferences, StartCandidate
from .osm import (
    _distinct_by_length,
    _edge_preference_cost,
    _nearest_node,
    _route_coordinates,
)
from .pools import FORMAT_VERSION, decode_pool
from .scoring import rank_candidates

LOGGER = logging.getLogger("runline.serving")

# A pool's loops start at the walkable node nearest its cell's centre, so the
# runner's own nearest node is usually within a cell's half-diagonal of it.
# Further than this along the streets, a connector would dominate a short loop.
MAX_CONNECTOR_METERS = 300.0


def pools_root() -> Path:
    override = os.environ.get("RUNLINE_POOLS_DIR")
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[2] / "pools"


@dataclass(frozen=True)
class PoolFile:
    path: Path
    grid_meters: float


@functools.lru_cache(maxsize=8)
def _checked_pool_file(path: str, graph_path: str, modified: float) -> PoolFile | None:
    """The pool file if it matches this code and this graph; cached per mtime."""

    try:
        database = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            meta = {key: json.loads(value) for key, value in database.execute("SELECT key, value FROM meta")}
        finally:
            database.close()
    except sqlite3.Error as error:
        LOGGER.warning("ignoring unreadable pool file %s: %s", path, error)
        return None
    if meta.get("format") != FORMAT_VERSION:
        LOGGER.warning("ignoring %s: pool format %s, expected %s", path, meta.get("format"), FORMAT_VERSION)
        return None
    # Pools store node ids, which only mean something on the graph they were
    # built from. The hash matches scripts/precompute_pools.py.
    fingerprint = hashlib.sha256(Path(graph_path).read_bytes()).hexdigest()[:16]
    if meta.get("graph") != fingerprint:
        LOGGER.warning("ignoring %s: built from graph %s, not %s", path, meta.get("graph"), fingerprint)
        return None
    return PoolFile(Path(path), float(meta["grid_meters"]))


def pool_file(area: Area) -> PoolFile | None:
    path = pools_root() / f"{area.slug}.sqlite"
    if not path.exists():
        return None
    return _checked_pool_file(str(path), str(graphs_root() / area.filename), path.stat().st_mtime)


def load_pool(pool: PoolFile, cell: str, surface: str) -> dict | None:
    database = sqlite3.connect(f"file:{pool.path}?mode=ro", uri=True)
    try:
        row = database.execute("SELECT pool FROM pools WHERE cell = ? AND surface = ?", (cell, surface)).fetchone()
    finally:
        database.close()
    return decode_pool(row[0]) if row else None


def _connector(graph: nx.MultiDiGraph, start_node: int, origin_node: int) -> tuple[list[int], list[int], float] | None:
    """Street paths from the runner's node to the pool's origin and back, and the length."""

    if start_node == origin_node:
        return [start_node], [start_node], 0.0
    try:
        out_length, out = nx.bidirectional_dijkstra(graph, start_node, origin_node, weight="length")
        _, back = nx.bidirectional_dijkstra(graph, origin_node, start_node, weight="length")
    except nx.NetworkXNoPath:
        return None
    if out_length > MAX_CONNECTOR_METERS:
        return None
    return out, back, float(out_length)


def _with_connector(metrics: dict, connector_meters: float) -> RouteMetrics:
    """A pool loop's metrics once it runs the connector out and back."""

    if not connector_meters:
        return RouteMetrics(**metrics)
    miles = metrics["distance_miles"]
    added = 2 * connector_meters / METERS_PER_MILE
    # One of the two passes repeats the other.
    repeated_miles = metrics["repeated_fraction"] * miles + added / 2
    return RouteMetrics(
        **{
            **metrics,
            "distance_miles": miles + added,
            "repeated_fraction": min(1.0, repeated_miles / (miles + added)),
        }
    )


def _route_graph(graph: nx.MultiDiGraph, routes: list[tuple[int, ...]], preferences: RoutePreferences) -> nx.DiGraph:
    """Only the edges these routes use, each the cheapest parallel edge for the runner.

    The same choice collapse_graph makes for the whole area, which takes
    seconds; this takes a millisecond and is all that measuring overlap and
    drawing the routes need.
    """

    small = nx.DiGraph()
    for nodes in routes:
        for u, v in zip(nodes, nodes[1:]):
            if small.has_edge(u, v):
                continue
            best = min(graph[u][v].values(), key=lambda data: _edge_preference_cost(data, graph.nodes[v], preferences))
            small.add_edge(u, v, **best)
            small.nodes[u].update(graph.nodes[u])
            small.nodes[v].update(graph.nodes[v])
    return small


def serve_from_pool(origin: Coordinate, preferences: RoutePreferences) -> list[RouteCandidate] | None:
    """Ranked routes from the origin's pool, or None if live planning should answer."""

    if preferences.drive_radius_miles:
        return None
    area = containing_area(origin)
    if area is None:
        return None
    pool_source = pool_file(area)
    if pool_source is None:
        return None
    cell = cell_id(origin.latitude, origin.longitude, pool_source.grid_meters)
    pool = load_pool(pool_source, cell, preferences.surface.value)
    if pool is None:
        return None

    graph = load_area_graph(area)
    start_node = _nearest_node(graph, origin)
    connector = _connector(graph, start_node, pool["origin_node"])
    if connector is None:
        return None
    out, back, connector_meters = connector

    in_band: list[RouteCandidate] = []
    for index, loop in enumerate(pool["loops"]):
        metrics = _with_connector(loop["metrics"], connector_meters)
        if abs(metrics.distance_miles - preferences.target_distance_miles) > preferences.distance_tolerance_miles:
            continue
        nodes = (*out, *loop["nodes"][1:], *back[1:])
        in_band.append(RouteCandidate(f"pool-{index}", nodes, (), metrics))
    if len(in_band) < preferences.result_count:
        return None

    try:
        small = _route_graph(graph, [candidate.node_ids for candidate in in_band], preferences)
    except KeyError as error:
        # Checked by hash above, so this would be a corrupt pool, not a stale one.
        LOGGER.warning("pool %s %s names a missing edge: %s", cell, preferences.surface.value, error)
        return None
    ranked = _distinct_by_length(small, rank_candidates(in_band, preferences), preferences.result_count)
    if len(ranked) < preferences.result_count:
        return None

    start = StartCandidate("Start here", "origin", origin, 0)
    for position, candidate in enumerate(ranked, start=1):
        candidate.route_id = f"route-{position:03d}"
        candidate.coordinates = _route_coordinates(small, list(candidate.node_ids))
        candidate.start = start
    return ranked
