"""Metros precomputed on tiles: which tile a start belongs to, and its graph.

A metro run (scripts/plan_tile_pools.py, precompute_pools.py --plan,
merge_pools.py --plan) ships as one directory under RUNLINE_METROS_DIR
(default ``metros/`` in the repo):

    <run>/pools.sqlite     the merged pools, with the cell index
    <run>/tile-graphs/     <tile>.pkl.gz from build_tile_graphs.py

``tile-graphs`` may be a symlink, so batches can share one directory of tile
graphs. A run can hold several metros (a batch) and a metro's tiles can sit
in the run of a neighbouring metro's batch; every start goes to the tile
that owns its cell, by the ``cells`` table merge_pools.py writes from the
plan.

A pool's node ids only mean something on its own tile's graph, so serving
loads that graph. The top 50 metros have about 8,700 tiles, far too many to
hold at once, so graphs load on first use and at most
RUNLINE_TILE_GRAPHS_CACHE (default 2) stay in memory. scripts/
measure_tile_graphs.py measures what one costs, to size the server.

A tile is handed to the rest of the code as an ``Area`` whose disc is the
tile's, so live planning treats it like a shipped area: runline.coverage
asks this module after the manifest's areas, which keep priority.

With RUNLINE_TILE_GRAPHS_URL set, a tile graph missing from ``tile-graphs/``
is fetched into a bounded disk cache on first use (runline.tile_fetch).
Unset, only the run's own files are read.
"""

from __future__ import annotations

import gzip
import json
import logging
import os
import pickle
import sqlite3
import threading
from collections import OrderedDict
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import networkx as nx

from .coverage import Area, CoverageError
from .geo import destination
from .grid import cell_id
from .models import Coordinate
from .tile_fetch import TileGraphFetcher, configured_url

LOGGER = logging.getLogger("runline.metros")

POOLS_NAME = "pools.sqlite"
TILE_GRAPHS_NAME = "tile-graphs"
# The cells/tiles layout this code reads, written by scripts/merge_pools.py.
METRO_INDEX_VERSION = 1
DEFAULT_TILE_GRAPHS_CACHE = 2


def metros_root() -> Path:
    override = os.environ.get("RUNLINE_METROS_DIR")
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[2] / "metros"


def tile_graphs_cache_size() -> int:
    try:
        return max(1, int(os.environ.get("RUNLINE_TILE_GRAPHS_CACHE", DEFAULT_TILE_GRAPHS_CACHE)))
    except ValueError:
        return DEFAULT_TILE_GRAPHS_CACHE


@dataclass(frozen=True)
class Tile:
    slug: str
    metro: str
    label: str
    graph: str | None  # hash of the graph its pools were built on; None if it has no streets
    center: Coordinate
    radius_meters: float


@dataclass(frozen=True, eq=False)
class MetroRun:
    pools: Path
    graphs: Path
    grid_meters: float
    labels: dict[str, str]
    tiles: dict[str, Tile]


def _read_run(path: str, modified: float) -> MetroRun | None:
    """A run's index if its pools file has one; cached per mtime by ``_run``."""

    try:
        database = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            meta = {key: json.loads(value) for key, value in database.execute("SELECT key, value FROM meta")}
            if meta.get("metro_index") != METRO_INDEX_VERSION:
                LOGGER.warning(
                    "ignoring %s: metro index %s, expected %s; run scripts/merge_pools.py --plan <its plan> --add-index %s",
                    path, meta.get("metro_index"), METRO_INDEX_VERSION, path,
                )
                return None
            rows = database.execute(
                "SELECT tile, graph, metro, latitude, longitude, radius_meters FROM tiles"
            ).fetchall()
        finally:
            database.close()
    except sqlite3.Error as error:
        LOGGER.warning("ignoring unreadable metro file %s: %s", path, error)
        return None
    labels = dict(meta.get("metros", {}))
    tiles = {
        slug: Tile(slug, metro, labels.get(metro, metro), graph, Coordinate(latitude, longitude), radius)
        for slug, graph, metro, latitude, longitude, radius in rows
    }
    return MetroRun(Path(path), Path(path).parent / TILE_GRAPHS_NAME, float(meta["grid_meters"]), labels, tiles)


_run = lru_cache(maxsize=32)(_read_run)


def metro_runs() -> tuple[MetroRun, ...]:
    """Every metro run with a usable index, in directory order."""

    root = metros_root()
    if not root.is_dir():
        return ()
    runs = []
    for path in sorted(root.glob(f"*/{POOLS_NAME}")):
        run = _run(str(path), path.stat().st_mtime)
        if run is not None:
            runs.append(run)
    return tuple(runs)


def metro_labels() -> tuple[str, ...]:
    return tuple(dict.fromkeys(label for run in metro_runs() for label in run.labels.values()))


@lru_cache(maxsize=8192)
def _cell_tile(path: str, modified: float, cell: str) -> str | None:
    database = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        row = database.execute("SELECT tile FROM cells WHERE cell = ?", (cell,)).fetchone()
    finally:
        database.close()
    return row[0] if row else None


def _owning_tile(run: MetroRun, origin: Coordinate) -> Tile | None:
    """The tile that owns the origin's cell, or failing that a neighbouring cell's.

    Planned cells reach a cell's half-diagonal past the metro's boundary, so
    the neighbours only matter for a start right at the edge, or for the
    rounded origin live planning crops around (osm.prepare_core).
    """

    modified = run.pools.stat().st_mtime
    points = [origin, *(destination(origin, bearing, run.grid_meters) for bearing in range(0, 360, 45))]
    for point in points:
        slug = _cell_tile(str(run.pools), modified, cell_id(point.latitude, point.longitude, run.grid_meters))
        if slug is not None and slug in run.tiles:
            return run.tiles[slug]
    return None


def tile_area(origin: Coordinate) -> Area | None:
    """The owning tile of the origin as an Area, or None outside every metro.

    A tile with no streets (all water) has no graph, so it covers nothing. A
    tile whose graph file is missing is still returned: loading it raises a
    CoverageError naming the metro, which says more than "no map data".
    """

    for run in metro_runs():
        tile = _owning_tile(run, origin)
        if tile is None:
            continue
        if tile.graph is None:
            return None
        return Area(
            slug=tile.slug,
            label=tile.label,
            center=tile.center,
            radius_meters=tile.radius_meters,
            filename=str(run.graphs / f"{tile.slug}.pkl.gz"),
            metro_pools=str(run.pools),
        )
    return None


def tile_for(area: Area) -> Tile | None:
    """The Tile record behind a tile Area, for its graph hash."""

    if not area.metro_pools:
        return None
    path = Path(area.metro_pools)
    if not path.exists():
        return None
    run = _run(str(path), path.stat().st_mtime)
    return run.tiles.get(area.slug) if run else None


def read_graph_file(path: Path) -> nx.MultiDiGraph:
    """Unpickle a tile graph, streaming so the compressed bytes are never all in memory."""

    with gzip.open(path, "rb") as handle:
        return pickle.load(handle)


class _TileGraphs:
    """Tile graphs by path, least recently used first, at most ``size`` held.

    A graph is loaded once even when several requests want it at the same
    time, so memory never holds two copies; requests for other tiles do not
    wait on it. A graph evicted while a request still uses it lives until
    that request ends, so the bound is the cache size plus in-flight loads.
    """

    def __init__(self) -> None:
        self._graphs: OrderedDict[tuple[str, float], nx.MultiDiGraph] = OrderedDict()
        self._lock = threading.Lock()
        self._loading: dict[tuple[str, float], threading.Lock] = {}
        self.loads = 0

    def get(self, path: Path) -> nx.MultiDiGraph:
        key = (str(path), path.stat().st_mtime)
        with self._lock:
            graph = self._graphs.get(key)
            if graph is not None:
                self._graphs.move_to_end(key)
                return graph
            loading = self._loading.setdefault(key, threading.Lock())
        with loading:
            with self._lock:
                graph = self._graphs.get(key)
                if graph is not None:
                    self._graphs.move_to_end(key)
                    return graph
            graph = read_graph_file(path)
            with self._lock:
                self.loads += 1
                self._graphs[key] = graph
                self._loading.pop(key, None)
                while len(self._graphs) > tile_graphs_cache_size():
                    self._graphs.popitem(last=False)
            return graph

    def held(self) -> int:
        return len(self._graphs)

    def held_paths(self) -> set[str]:
        """Files of the graphs in memory or loading, which the disk cache must keep."""

        with self._lock:
            return {path for path, _ in (*self._graphs, *self._loading)}

    def clear(self) -> None:
        with self._lock:
            self._graphs.clear()
            self.loads = 0


TILE_GRAPHS = _TileGraphs()
FETCHER = TileGraphFetcher()


@contextmanager
def tile_graph_file(area: Area) -> Iterator[Path]:
    """The tile's graph file: the run's own, else a fetched copy when fetching is on.

    The path may not exist, as when the run lacks it and fetching is off. A
    fetched copy is kept on disk until the block ends. Raises
    MapDataUnavailable if fetching is on and the download fails.
    """

    path = Path(area.filename)
    tile = None if path.exists() or not configured_url() else tile_for(area)
    if tile is None or tile.graph is None:
        yield path
        return
    run = Path(area.metro_pools).parent.name
    with FETCHER.use(run, tile.slug, tile.graph, area.label, TILE_GRAPHS.held_paths) as fetched:
        yield fetched


def load_tile_graph(area: Area) -> nx.MultiDiGraph:
    with tile_graph_file(area) as path:
        if not path.exists():
            raise CoverageError(f"Map data for {area.label} is missing from this deployment.")
        loads = TILE_GRAPHS.loads
        graph = TILE_GRAPHS.get(path)
        fetched = path != Path(area.filename)
    if fetched and TILE_GRAPHS.loads != loads:
        # A cold load can push another fetched graph out of memory, which
        # frees its file for the disk cache to evict.
        FETCHER.evict(TILE_GRAPHS.held_paths())
    return graph
