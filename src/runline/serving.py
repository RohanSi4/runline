"""Answer route requests from precomputed pools instead of searching live.

A request's start point names a grid cell; that cell's pool (built by
scripts/precompute_pools.py, merged by scripts/merge_pools.py) already holds
loops at every whole-mile target, as do the eight cells around it. Serving
joins each loop where it passes closest to the runner, keeps the ones that
come out near the requested distance, measures them as drawn, ranks them with
the same scoring live routes use, and draws the winners.

Pools are an optimization, never a requirement. ``serve_from_pool`` returns
None whenever it cannot give a usable answer (no file, an old format, a pool
built from a different graph, a cell with no pool, too few loops in the
distance band that don't double back, a drive-radius request) and the planner
searches live.

The default request asks for up to three routes. Two distinct, in-band
routes are a usable answer; one triggers live planning for more choice.

A start inside a metro precomputed on tiles (runline.metros) resolves the
same way: its cell names the owning tile, ``containing_area`` returns that
tile as an Area, ``pool_file`` checks the metro's file and the tile's graph
hash, and ``load_area_graph`` loads that tile's graph, which is the one the
pool's node ids belong to. On a miss, live planning runs on the same tile.
"""

from __future__ import annotations

import dataclasses
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
from .grid import cell_id, neighbouring_cells
from .metros import tile_for
from .models import METERS_PER_MILE, Coordinate, RouteCandidate, RouteMetrics, RoutePreferences, StartCandidate
from .osm import (
    _distinct_by_length,
    _edge_preference_cost,
    _is_major,
    _nearest_node,
    _route_coordinates,
    _trim_spurs,
    measure_route,
)
from .pools import FORMAT_VERSION, decode_pool
from .scoring import MAX_REPEATED, rank_candidates

LOGGER = logging.getLogger("runline.serving")

# A runner joins a pool loop where it passes closest along the streets. Further
# than this each way, the connector would dominate a short loop.
MAX_CONNECTOR_METERS = 300.0
# Loops are filtered on their stored length before being measured as drawn,
# which can pick another of two parallel edges; this keeps the borderline ones.
BAND_SLACK_MILES = 0.05

# A pool keeps ten loops per target, so a request's distance band can hold
# few good ones and ranking would serve one that doubles back. Loops past the
# ranking's limit are left out, and with too few left the planner searches
# live, which has many more loops to choose from.
MAX_DOUBLED_BACK = MAX_REPEATED


def pools_root() -> Path:
    override = os.environ.get("RUNLINE_POOLS_DIR")
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[2] / "pools"


@dataclass(frozen=True)
class PoolFile:
    path: Path
    grid_meters: float
    # A metro file's rows for one tile only: the pool must be the one built
    # on the graph serving loads.
    tile: str | None = None


def _current_meta(path: str) -> dict | None:
    """A pool file's meta if it is readable and in this code's pool format."""

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
    return meta


def _fingerprint(path: Path) -> str:
    """The graph hash scripts/precompute_pools.py records, read in chunks."""

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()[:16]


@functools.lru_cache(maxsize=8)
def _checked_pool_file(path: str, graph_path: str, modified: float) -> PoolFile | None:
    """The pool file if it matches this code and this graph; cached per mtime."""

    meta = _current_meta(path)
    if meta is None:
        return None
    # Pools store node ids, which only mean something on the graph they were
    # built from. The hash matches scripts/precompute_pools.py.
    fingerprint = _fingerprint(Path(graph_path))
    if meta.get("graph") != fingerprint:
        LOGGER.warning("ignoring %s: built from graph %s, not %s", path, meta.get("graph"), fingerprint)
        return None
    return PoolFile(Path(path), float(meta["grid_meters"]))


@functools.lru_cache(maxsize=8)
def _checked_metro_file(path: str, modified: float) -> PoolFile | None:
    """A metro's merged file if its pools are in this code's format; cached per mtime."""

    meta = _current_meta(path)
    return PoolFile(Path(path), float(meta["grid_meters"])) if meta else None


@functools.lru_cache(maxsize=4096)
def _tile_graph_matches(pools: str, tile: str, expected: str, graph_path: str, modified: float) -> bool:
    """Whether a tile graph is the one its pools were built on; hashed once per mtime."""

    fingerprint = _fingerprint(Path(graph_path))
    if fingerprint != expected:
        LOGGER.warning("ignoring %s pools in %s: built from graph %s, not %s", tile, pools, expected, fingerprint)
    return fingerprint == expected


def _tile_pool_file(area: Area) -> PoolFile | None:
    """The metro file for a tile Area, if its pools fit this code and the tile's graph."""

    tile = tile_for(area)
    graph_path = Path(area.filename)
    if tile is None or tile.graph is None or not graph_path.exists():
        return None
    pools = _checked_metro_file(area.metro_pools, Path(area.metro_pools).stat().st_mtime)
    if pools is None:
        return None
    # Each tile has its own graph; the merge recorded the hash of each.
    if not _tile_graph_matches(area.metro_pools, tile.slug, tile.graph, str(graph_path), graph_path.stat().st_mtime):
        return None
    return dataclasses.replace(pools, tile=tile.slug)


def pool_file(area: Area) -> PoolFile | None:
    if area.metro_pools:
        return _tile_pool_file(area)
    path = pools_root() / f"{area.slug}.sqlite"
    if not path.exists():
        return None
    return _checked_pool_file(str(path), str(graphs_root() / area.filename), path.stat().st_mtime)


def load_pool(pool: PoolFile, cell: str, surface: str) -> dict | None:
    loaded = load_pools(pool, [cell], surface)
    return loaded[0] if loaded else None


def load_pools(pool: PoolFile, cells: list[str], surface: str) -> list[dict]:
    """Pools in cell order, restricted to the graph being served.

    Neighbouring metro cells can belong to another tile. Their loops must
    stay on that tile's graph even when some OSM node ids appear in both.
    """

    if not cells:
        return []
    database = sqlite3.connect(f"file:{pool.path}?mode=ro", uri=True)
    try:
        tile_filter = " AND tile = ?" if pool.tile is not None else ""
        parameters = (surface, *cells) + ((pool.tile,) if pool.tile is not None else ())
        rows = dict(
            database.execute(
                f"SELECT cell, pool FROM pools WHERE surface = ? AND cell IN ({', '.join('?' * len(cells))}){tile_filter}",
                parameters,
            ).fetchall()
        )
    finally:
        database.close()
    return [decode_pool(rows[cell]) for cell in cells if cell in rows]


def _miss(reason: str, cell: str, surface: str) -> None:
    """Log why a request found a pool but could not use it; returns None."""

    LOGGER.debug("pool %s %s cannot answer: %s", cell, surface, reason, extra={"pool_miss": reason})
    return None


@dataclass(frozen=True)
class _Reach:
    """Shortest street paths, by length, from the runner's node and back to it."""

    out_meters: dict[int, float]
    out_paths: dict[int, list[int]]
    back_meters: dict[int, float]
    back_paths: dict[int, list[int]]


def _reach(graph: nx.MultiDiGraph, start_node: int) -> _Reach:
    out_meters, out_paths = nx.single_source_dijkstra(
        graph, start_node, cutoff=MAX_CONNECTOR_METERS, weight="length"
    )
    # Searched backwards, so each path runs from the start; reversed on use.
    back_meters, back_paths = nx.single_source_dijkstra(
        graph.reverse(copy=False), start_node, cutoff=MAX_CONNECTOR_METERS, weight="length"
    )
    return _Reach(out_meters, out_paths, back_meters, back_paths)


def _anchored(loop: list[int], reach: _Reach) -> tuple[tuple[int, ...], float] | None:
    """The loop restarted where it passes closest to the runner, with the way there and back.

    Every node of a loop is somewhere it could start, so the runner joins it
    at whichever node is the shortest round trip away instead of at the pool's
    origin. Returns the route's nodes and the connector's out-and-back length,
    or None if the loop never comes within MAX_CONNECTOR_METERS each way.
    """

    best = None
    for position, node in enumerate(loop[:-1]):
        if node in reach.out_meters and node in reach.back_meters:
            meters = reach.out_meters[node] + reach.back_meters[node]
            if best is None or meters < best[0]:
                best = (meters, position, node)
    if best is None:
        return None
    meters, position, node = best
    restarted = [*loop[position:-1], *loop[: position + 1]]
    nodes = [*reach.out_paths[node][:-1], *restarted, *reversed(reach.back_paths[node][:-1])]
    # Where the way out joins the loop at a node the loop then leaves along,
    # the runner would turn straight back; drop that reversal.
    return tuple(_trim_spurs(nodes)), meters


MAJOR_AT_KEY = "_runline_major_at"


def _route_graph(graph: nx.MultiDiGraph, routes: list[tuple[int, ...]], preferences: RoutePreferences) -> nx.DiGraph:
    """The edges these routes use, each the cheapest parallel edge for the
    runner, plus every major-road edge at a node they pass.

    The same choice collapse_graph makes for the whole area, which takes
    seconds; this takes milliseconds. The major roads let measure_route count
    crossings of roads the route does not walk along.
    """

    small = nx.DiGraph()

    def add(u: int, v: int) -> None:
        if small.has_edge(u, v):
            return
        best = min(
            graph[u][v].values(),
            key=lambda data: _edge_preference_cost(data, graph.nodes[v], preferences, graph.nodes[u]),
        )
        small.add_edge(u, v, **best)
        small.nodes[u].update(graph.nodes[u])
        small.nodes[v].update(graph.nodes[v])

    nodes = set()
    for route in routes:
        for u, v in zip(route, route[1:]):
            add(u, v)
        nodes.update(route)
    # Memoized on the area graph, which stays loaded between requests.
    major_at = graph.graph.setdefault(MAJOR_AT_KEY, {})
    for node in nodes:
        pairs = major_at.get(node)
        if pairs is None:
            pairs = major_at[node] = tuple(
                (u, v)
                for u, v in {*graph.in_edges(node), *graph.out_edges(node)}
                if any(_is_major(data) for data in graph[u][v].values())
            )
        for u, v in pairs:
            add(u, v)
    return small


def _candidates(pools: list[dict], reach: _Reach, preferences: RoutePreferences) -> list[RouteCandidate]:
    """Pool loops that, joined from the runner, could fit the distance band.

    Lengths here are the stored loop's plus the connector's; the exact length
    is measured once the route is drawn.
    """

    target = preferences.target_distance_miles
    tolerance = preferences.distance_tolerance_miles + BAND_SLACK_MILES
    longest_connector_miles = 2 * MAX_CONNECTOR_METERS / METERS_PER_MILE
    seen: set[tuple[int, ...]] = set()
    candidates: list[RouteCandidate] = []
    for pool in pools:
        for loop in pool["loops"]:
            miles = loop["metrics"]["distance_miles"]
            if not target - tolerance - longest_connector_miles <= miles <= target + tolerance:
                continue
            anchored = _anchored(loop["nodes"], reach)
            if anchored is None:
                continue
            nodes, connector_meters = anchored
            if abs(miles + connector_meters / METERS_PER_MILE - target) > tolerance or nodes in seen:
                continue
            seen.add(nodes)
            candidates.append(RouteCandidate(f"pool-{len(seen)}", nodes, (), RouteMetrics(**loop["metrics"])))
    return candidates


def _measured(small: nx.DiGraph, candidates: list[RouteCandidate], preferences: RoutePreferences) -> list[RouteCandidate]:
    """The candidates in the distance band as drawn, connector included.

    Signals, crossings, climbing and doubling back on the connector count like
    anywhere else on the route, as live planning measures them.
    """

    measured = []
    for candidate in candidates:
        metrics = measure_route(small, list(candidate.node_ids))
        if abs(metrics.distance_miles - preferences.target_distance_miles) <= preferences.distance_tolerance_miles:
            measured.append(dataclasses.replace(candidate, metrics=metrics))
    return measured


def _ranked(small: nx.DiGraph, measured: list[RouteCandidate], preferences: RoutePreferences) -> tuple[list, str]:
    """The top routes, or none and the reason there are too few."""

    count = preferences.result_count
    minimum = 2 if count == 3 else count
    if len(measured) < minimum:
        return [], "few in band"
    kept = [candidate for candidate in measured if candidate.metrics.repeated_fraction <= MAX_DOUBLED_BACK]
    if len(kept) < minimum:
        return [], "few without doubling back"
    ranked = _distinct_by_length(small, rank_candidates(kept, preferences), count)
    if len(ranked) < minimum:
        return [], "few distinct"
    return ranked, ""


def serve_from_pool(origin: Coordinate, preferences: RoutePreferences) -> list[RouteCandidate] | None:
    """Ranked routes from the pools around the origin, or None if live planning should answer."""

    if preferences.drive_radius_miles:
        return None
    area = containing_area(origin)
    if area is None:
        return None
    pool_source = pool_file(area)
    if pool_source is None:
        return None
    cell = cell_id(origin.latitude, origin.longitude, pool_source.grid_meters)
    surface = preferences.surface.value
    # Loops from the cells around pass the runner too. Reading all nine found
    # ~45 candidates a request instead of ~10, and cut crossings per mile 30%
    # for ~50 ms (docs/routing-study.md, "Pool serving").
    pools = load_pools(pool_source, neighbouring_cells(cell), surface)
    if not pools:
        return None

    graph = load_area_graph(area)
    reach = _reach(graph, _nearest_node(graph, origin))
    candidates = _candidates(pools, reach, preferences)
    if not candidates:
        return _miss("no loop within reach", cell, surface)
    try:
        small = _route_graph(graph, [candidate.node_ids for candidate in candidates], preferences)
    except KeyError as error:
        # Checked by hash above, so this would be a corrupt pool, not a stale one.
        LOGGER.warning("pool near %s %s names a missing edge: %s", cell, surface, error)
        return None
    ranked, reason = _ranked(small, _measured(small, candidates, preferences), preferences)
    if not ranked:
        return _miss(reason, cell, surface)

    start = StartCandidate("Start here", "origin", origin, 0)
    for position, candidate in enumerate(ranked, start=1):
        candidate.route_id = f"route-{position:03d}"
        candidate.start = start
        candidate.coordinates = _route_coordinates(small, list(candidate.node_ids))
    return ranked
