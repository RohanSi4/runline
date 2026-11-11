"""Metro pools: one merged run of many tiles, each tile with its own graph.

An area pool (``pools/<area>.sqlite``) is built on one graph, named by its
``graph`` hash in ``meta``. A metro run (scripts/merge_pools.py --plan) covers
a metro with overlapping tiles (runline.tiling), and each cell's pool was
computed on its owning tile's graph, so its node ids only mean something there.
Such a file has no ``graph`` in ``meta``; instead ``tiles`` holds each tile's
graph hash and ``cells`` names the tile that owns each cell.

Serving finds the file and tile for a start from ``cells``, then loads that
tile's graph (``<tile graphs>/<tile>.pkl.gz``, from build_tile_graphs.py),
which also lets a cell with no usable pool plan live on the same map.

Every ``*.sqlite`` in the pools directory that has a ``cells`` table is a metro
file. Tile graphs default to ``<pools>/tile-graphs``; set
``RUNLINE_TILE_GRAPHS_DIR`` to keep them elsewhere. A handful of tile graphs
are kept loaded (``RUNLINE_TILE_CACHE``, default 4); a request elsewhere loads
its tile from disk.
"""

from __future__ import annotations

import functools
import gzip
import hashlib
import json
import logging
import os
import pickle
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import networkx as nx

from .grid import cell_id
from .models import Coordinate
from .pools import FORMAT_VERSION

LOGGER = logging.getLogger("runline.metros")

ROOT = Path(__file__).resolve().parents[2]
TILE_CACHE_SIZE = int(os.environ.get("RUNLINE_TILE_CACHE", "4"))


def pools_root() -> Path:
    override = os.environ.get("RUNLINE_POOLS_DIR")
    if override:
        return Path(override)
    return ROOT / "pools"


def tile_graphs_root() -> Path:
    override = os.environ.get("RUNLINE_TILE_GRAPHS_DIR")
    if override:
        return Path(override)
    return pools_root() / "tile-graphs"


@dataclass(frozen=True)
class MetroPool:
    path: Path
    grid_meters: float
    metros: tuple[str, ...]


@dataclass(frozen=True)
class TileMatch:
    pool: MetroPool
    tile: str
    cell: str


@functools.lru_cache(maxsize=64)
def _read_metro_pool(path: str, modified: float) -> MetroPool | None:
    """The file as a metro pool, or None if it is an area pool or unusable."""

    try:
        database = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            tables = {name for (name,) in database.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            meta = {key: json.loads(value) for key, value in database.execute("SELECT key, value FROM meta")}
        finally:
            database.close()
    except sqlite3.Error as error:
        LOGGER.warning("ignoring unreadable pool file %s: %s", path, error)
        return None
    if "graph" in meta:
        return None  # an area pool, served by runline.serving
    if "cells" not in tables:
        if "tiles" in tables:
            LOGGER.warning("ignoring %s: no cells table; run scripts/index_pool_tiles.py on it", path)
        return None
    if meta.get("format") != FORMAT_VERSION:
        LOGGER.warning("ignoring %s: pool format %s, expected %s", path, meta.get("format"), FORMAT_VERSION)
        return None
    return MetroPool(Path(path), float(meta["grid_meters"]), tuple(meta.get("metros", ())))


def metro_pools() -> tuple[MetroPool, ...]:
    root = pools_root()
    if not root.is_dir():
        return ()
    found = (_read_metro_pool(str(path), path.stat().st_mtime) for path in sorted(root.glob("*.sqlite")))
    return tuple(pool for pool in found if pool is not None)


def find_tile(origin: Coordinate) -> TileMatch | None:
    """The metro file and tile that own the origin's cell, if any does."""

    for pool in metro_pools():
        cell = cell_id(origin.latitude, origin.longitude, pool.grid_meters)
        database = sqlite3.connect(f"file:{pool.path}?mode=ro", uri=True)
        try:
            row = database.execute("SELECT tile FROM cells WHERE cell = ?", (cell,)).fetchone()
        finally:
            database.close()
        if row:
            return TileMatch(pool, row[0], cell)
    return None


@functools.lru_cache(maxsize=TILE_CACHE_SIZE)
def _load_tile_file(path: str, modified: float) -> tuple[nx.MultiDiGraph, str]:
    # Hashed from the bytes it is loaded from, as precompute_pools.py does.
    # The pickles are build artifacts from our own cluster run: trusted input.
    payload = Path(path).read_bytes()
    return pickle.loads(gzip.decompress(payload)), hashlib.sha256(payload).hexdigest()[:16]


def load_tile(match: TileMatch) -> tuple[nx.MultiDiGraph, bool] | None:
    """The tile's graph and whether the pools were built from this exact graph."""

    path = tile_graphs_root() / f"{match.tile}.pkl.gz"
    if not path.exists():
        LOGGER.warning("no graph for tile %s at %s", match.tile, path)
        return None
    graph, fingerprint = _load_tile_file(str(path), path.stat().st_mtime)
    database = sqlite3.connect(f"file:{match.pool.path}?mode=ro", uri=True)
    try:
        row = database.execute("SELECT graph FROM tiles WHERE tile = ?", (match.tile,)).fetchone()
    finally:
        database.close()
    matches = bool(row) and row[0] == fingerprint
    if not matches:
        LOGGER.warning("tile %s graph is %s, pools were built from %s", match.tile, fingerprint, row[0] if row else None)
    return graph, matches


def metro_tile_graph(origin: Coordinate) -> nx.MultiDiGraph | None:
    """The walk graph of the tile owning the origin's cell, for live planning."""

    match = find_tile(origin)
    if match is None:
        return None
    loaded = load_tile(match)
    return loaded[0] if loaded else None


@functools.lru_cache(maxsize=1)
def metro_tile_radius_meters() -> float:
    """Crop radius a tile guarantees for any start in a cell it owns."""

    from .tiling import DEFAULT_MAX_MILES, needed_radius_meters

    return needed_radius_meters(DEFAULT_MAX_MILES)


@functools.lru_cache(maxsize=1)
def _metro_names() -> dict[str, str]:
    path = ROOT / "config" / "metros.json"
    if not path.exists():
        return {}
    config = json.loads(path.read_text(encoding="utf-8"))
    return {metro["slug"]: metro["name"] for metro in config["metros"]}


def metro_labels() -> tuple[str, ...]:
    """Names of the metros a deployment's metro pools cover."""

    names = _metro_names()
    slugs = dict.fromkeys(slug for pool in metro_pools() for slug in pool.metros)
    return tuple(names.get(slug, slug) for slug in slugs)
