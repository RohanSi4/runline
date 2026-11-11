"""Metro pools: requests served on the graph of the tile that owns their cell.

The Charlottesville graph stands in for one tile of a metro run, with the
area manifest hidden so only the metro file can answer.
"""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import shutil
import sqlite3
from pathlib import Path

import pytest

from runline import metros, osm, serving
from runline.coverage import CoverageError, areas, graphs_root, load_area_graph
from runline.grid import cell_id
from runline.models import Coordinate, RoutePreferences, SurfacePreference
from runline.planner import plan_routes
from runline.pools import FORMAT_VERSION, encode_pool
from scripts import index_pool_tiles
from scripts.precompute_pools import cell_pool, prepare_area

TILE = "testmetro-000"
MILES = 5


@pytest.fixture(scope="module")
def built():
    area = next(item for item in areas() if item.slug == "charlottesville-va")
    graph = load_area_graph(area)
    cell = cell_id(area.center.latitude, area.center.longitude, 200.0)
    pool = cell_pool(prepare_area(graph, SurfacePreference.MIXED), cell, (4, 5, 6), SurfacePreference.MIXED)
    return area, cell, pool


def _write_metro(path: Path, cell: str, pool: dict, fingerprint: str, *, cells_table: bool = True) -> None:
    database = sqlite3.connect(path)
    database.executescript(
        """
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE tiles (tile TEXT PRIMARY KEY, graph TEXT) WITHOUT ROWID;
        CREATE TABLE pools (cell TEXT, surface TEXT, loops INTEGER, pool BLOB, PRIMARY KEY (cell, surface));
        """
    )
    meta = {"format": FORMAT_VERSION, "grid_meters": 200.0, "targets": [4, 5, 6]}
    if cells_table:
        database.execute("CREATE TABLE cells (cell TEXT PRIMARY KEY, tile TEXT NOT NULL) WITHOUT ROWID")
        database.execute("INSERT INTO cells VALUES (?, ?)", (cell, TILE))
        meta["metros"] = ["testmetro"]
    database.executemany("INSERT INTO meta VALUES (?, ?)", [(k, json.dumps(v)) for k, v in meta.items()])
    database.execute("INSERT INTO tiles VALUES (?, ?)", (TILE, fingerprint))
    database.execute("INSERT INTO pools VALUES (?, ?, ?, ?)", (cell, "mixed", len(pool["loops"]), encode_pool(pool)))
    database.commit()
    database.close()


@pytest.fixture
def metro(built, tmp_path: Path, monkeypatch) -> dict:
    """A metro pool file and tile graph, with no area map shipped."""

    area, cell, pool = built
    pools = tmp_path / "pools"
    tiles = pools / "tile-graphs"
    tiles.mkdir(parents=True)
    graph_file = tiles / f"{TILE}.pkl.gz"
    shutil.copy(graphs_root() / area.filename, graph_file)
    fingerprint = hashlib.sha256(graph_file.read_bytes()).hexdigest()[:16]
    _write_metro(pools / "testmetro.sqlite", cell, pool, fingerprint)
    (tmp_path / "graphs").mkdir()
    monkeypatch.setenv("RUNLINE_POOLS_DIR", str(pools))
    monkeypatch.setenv("RUNLINE_GRAPHS_DIR", str(tmp_path / "graphs"))
    monkeypatch.setenv("RUNLINE_ALLOW_OSM_DOWNLOAD", "0")
    metros._read_metro_pool.cache_clear()
    metros._load_tile_file.cache_clear()
    osm._PREPARED_CACHE.clear()
    return {"pools": pools, "tiles": tiles, "cell": cell, "pool": pool, "origin": Coordinate(*pool["origin"])}


def _preferences(**changes) -> RoutePreferences:
    return RoutePreferences(MILES, surface=SurfacePreference.MIXED, **changes)


def test_a_metro_cell_is_served_on_its_tile_graph(metro) -> None:
    match = metros.find_tile(metro["origin"])
    assert match.tile == TILE and match.cell == metro["cell"]

    routes = serving.serve_from_pool(metro["origin"], _preferences())
    assert routes and len(routes) == 3
    assert all(abs(route.metrics.distance_miles - MILES) <= _preferences().distance_tolerance_miles for route in routes)
    assert all(len(route.coordinates) > 10 for route in routes)


def test_the_planner_and_api_report_a_metro_pool(metro) -> None:
    from runline.api import RouteRequest, routes, supported_areas

    origin = metro["origin"]
    payload = asyncio.run(
        routes(RouteRequest(latitude=origin.latitude, longitude=origin.longitude, distance_miles=MILES, surface="mixed"))
    )
    assert payload["source"] == "pool" and len(payload["routes"]) == 3
    # No config entry is needed for a label; the slug stands in.
    assert asyncio.run(supported_areas())["areas"] == ["testmetro"]


def test_a_miss_in_a_metro_plans_live_on_the_tile(metro, tmp_path: Path) -> None:
    # Another surface has no pool, so the planner searches the tile's graph.
    preferences = RoutePreferences(MILES, surface=SurfacePreference.ROAD)
    assert serving.serve_from_pool(metro["origin"], preferences) is None
    result = plan_routes(metro["origin"], preferences, cache_directory=tmp_path, elevation_source="none")
    assert result.source == "live" and result.candidates
    assert not any("limited" in warning for warning in result.warnings)


def test_a_long_request_in_a_metro_is_marked_limited(metro, tmp_path: Path) -> None:
    graph = osm.load_graph(metro["origin"], metros.metro_tile_radius_meters() + 1000, tmp_path)
    assert graph.graph.get("coverage_limited")
    graph = osm.load_graph(metro["origin"], metros.metro_tile_radius_meters(), tmp_path)
    assert not graph.graph.get("coverage_limited")


def test_a_start_outside_every_metro_cell_is_unsupported(metro, tmp_path: Path) -> None:
    elsewhere = Coordinate(metro["origin"].latitude + 0.05, metro["origin"].longitude)
    assert metros.find_tile(elsewhere) is None
    assert serving.serve_from_pool(elsewhere, _preferences()) is None
    with pytest.raises(CoverageError):
        plan_routes(elsewhere, _preferences(), cache_directory=tmp_path, elevation_source="none")


def test_a_rebuilt_tile_graph_is_not_used_for_pools(metro, tmp_path: Path) -> None:
    graph_file = metro["tiles"] / f"{TILE}.pkl.gz"
    graph_file.write_bytes(gzip.compress(gzip.decompress(graph_file.read_bytes()), compresslevel=1))
    # Same graph, different bytes: pools can't be trusted, live still works.
    assert serving.serve_from_pool(metro["origin"], _preferences()) is None
    result = plan_routes(metro["origin"], _preferences(), cache_directory=tmp_path, elevation_source="none")
    assert result.source == "live" and result.candidates


def test_a_missing_tile_graph_falls_back(metro) -> None:
    (metro["tiles"] / f"{TILE}.pkl.gz").unlink()
    assert serving.serve_from_pool(metro["origin"], _preferences()) is None
    assert metros.metro_tile_graph(metro["origin"]) is None


def test_a_metro_file_without_a_cell_index_is_ignored(metro, caplog) -> None:
    path = metro["pools"] / "testmetro.sqlite"
    path.unlink()
    _write_metro(path, metro["cell"], metro["pool"], "0" * 16, cells_table=False)
    metros._read_metro_pool.cache_clear()
    assert metros.metro_pools() == ()
    assert "index_pool_tiles" in caplog.text


def test_area_pool_files_are_not_metro_files(tmp_path: Path, monkeypatch) -> None:
    database = sqlite3.connect(tmp_path / "somewhere.sqlite")
    database.executescript("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);")
    database.execute("INSERT INTO meta VALUES ('graph', '\"abc\"')")
    database.commit()
    database.close()
    monkeypatch.setenv("RUNLINE_POOLS_DIR", str(tmp_path))
    metros._read_metro_pool.cache_clear()
    assert metros.metro_pools() == ()


def _plan(path: Path, units: list[dict]) -> Path:
    with gzip.open(path, "wt", encoding="utf-8") as stream:
        json.dump({"version": 1, "grid_meters": 200.0, "max_miles": 12.0, "metros": ["testmetro"], "units": units}, stream)
    return path


def test_indexing_an_old_metro_file_adds_every_planned_cell(metro, tmp_path: Path) -> None:
    path = metro["pools"] / "testmetro.sqlite"
    path.unlink()
    _write_metro(path, metro["cell"], metro["pool"], "0" * 16, cells_table=False)
    plan = _plan(tmp_path / "plan.json.gz", [{"id": f"{TILE}-p00", "tile": TILE, "cells": [metro["cell"], "200:0:0"]}])

    assert index_pool_tiles.main(["--plan", str(plan), str(path)]) == 0
    assert index_pool_tiles.main(["--plan", str(plan), str(path)]) == 0  # twice is fine
    database = sqlite3.connect(path)
    assert dict(database.execute("SELECT cell, tile FROM cells")) == {metro["cell"]: TILE, "200:0:0": TILE}
    assert json.loads(database.execute("SELECT value FROM meta WHERE key = 'metros'").fetchone()[0]) == ["testmetro"]
    database.close()


def test_indexing_refuses_a_plan_that_misses_pooled_cells(metro, tmp_path: Path) -> None:
    plan = _plan(tmp_path / "plan.json.gz", [{"id": f"{TILE}-p00", "tile": TILE, "cells": ["200:0:0"]}])
    assert index_pool_tiles.main(["--plan", str(plan), str(metro["pools"] / "testmetro.sqlite")]) == 1
