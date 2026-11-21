"""Serving a metro run end to end: plan tiles, build tile graphs, precompute, merge, serve.

Two toy metros sit either side of the Charlottesville map centre, with tiles
sized for 3 mile loops so each tile graph is a small crop of the shipped
graph. Every test runs with no shipped areas, so a start can only be served
through its metro tile.
"""

from __future__ import annotations

import asyncio
import dataclasses
import gzip
import http.server
import json
import logging
import math
import os
import pickle
import shutil
import sqlite3
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import pytest
from fastapi import HTTPException
from shapely import affinity
from shapely.geometry import Point, mapping

import networkx as nx

from runline import coverage, metros, serving, tile_fetch
from runline import point_to_point as direct
from runline.api import RouteRequest, capabilities, routes, supported_areas
from runline.coverage import CoverageError, areas, load_area_graph
from runline.geo import destination, distance_meters
from runline.grid import cell_centre, cell_id
from runline.models import Coordinate, RoutePreferences, SurfacePreference
from runline.osm import crop_to_radius
from runline.planner import plan_routes
from runline.pools import decode_pool
from runline.tiling import metro_label, plan_tiles
from scripts import merge_pools, plan_tile_pools, precompute_pools

MAX_MILES = 3.0
CORE_METERS = 1_000.0
NAMES = {"west-toy": "West Toy--Elsewhere, VA--MD", "east-toy": "East Toy, VA"}


def _circle(centre: Coordinate, radius_km: float):
    circle = Point(centre.longitude, centre.latitude).buffer(1.0, quad_segs=32)
    return affinity.scale(
        circle, radius_km / (111.32 * math.cos(math.radians(centre.latitude))), radius_km / 110.54
    )


@pytest.fixture(scope="module")
def run(tmp_path_factory) -> dict:
    root = tmp_path_factory.mktemp("metro")
    area = next(item for item in areas() if item.slug == "charlottesville-va")
    graph = load_area_graph(area)
    shapes = {
        "west-toy": _circle(destination(area.center, 270, 1500), 0.5),
        "east-toy": _circle(destination(area.center, 90, 1500), 0.5),
    }

    config = {"grid_meters": 200.0, "max_miles": MAX_MILES, "metros": []}
    for slug, shape in shapes.items():
        plan = plan_tiles(shape, slug, max_miles=MAX_MILES, core_meters=CORE_METERS)
        counts = np.bincount(plan.owner, minlength=len(plan.tiles))
        config["metros"].append(
            {
                "slug": slug,
                "name": NAMES[slug],
                "tiles": [tile.to_json(slug, NAMES[slug], int(count)) for tile, count in zip(plan.tiles, counts)],
            }
        )
    (root / "metros.json").write_text(json.dumps(config))
    boundaries = {
        "type": "FeatureCollection",
        "features": [
            {"type": "Feature", "properties": {"slug": slug}, "geometry": mapping(shape)}
            for slug, shape in shapes.items()
        ],
    }
    (root / "boundaries.geojson").write_text(json.dumps(boundaries))

    directory = root / "metros" / "toys"
    assert plan_tile_pools.main([
        "--metros", str(root / "metros.json"), "--boundaries", str(root / "boundaries.geojson"),
        "--output", str(directory),
    ]) == 0
    with gzip.open(directory / "plan.json.gz", "rt") as stream:
        plan = json.load(stream)

    # A tile graph is its disc of the map, as build_tile_graphs.py cuts it.
    graphs = directory / "tile-graphs"
    graphs.mkdir()
    for slug, tile in plan["tiles"].items():
        centre = Coordinate(tile["latitude"], tile["longitude"])
        cropped = crop_to_radius(graph, centre, tile["radius_meters"])
        (graphs / f"{slug}.pkl.gz").write_bytes(gzip.compress(pickle.dumps(cropped, protocol=5), 1))

    shards = root / "shards"
    assert precompute_pools.main([
        "--plan", str(directory / "plan.json.gz"), "--tile-graphs", str(graphs), "--output", str(shards),
        "--targets", "2", "3", "--surfaces", "mixed", "--limit-cells", "2",
    ]) == 0
    assert merge_pools.main([
        "--plan", str(directory / "plan.json.gz"), "--input", str(shards),
        "--output", str(directory / "pools.sqlite"),
    ]) == 0

    database = sqlite3.connect(directory / "pools.sqlite")
    pools = {cell: decode_pool(blob) for cell, blob in database.execute("SELECT cell, pool FROM pools")}
    database.close()
    owner = {cell: unit["tile"] for unit in plan["units"] for cell in unit["cells"]}
    return {"root": root, "directory": directory, "plan": plan, "pools": pools, "owner": owner, "config": config}


@pytest.fixture
def served(run, tmp_path, monkeypatch) -> dict:
    """The run as the only coverage: no manifest areas, downloads off."""

    monkeypatch.setenv("RUNLINE_METROS_DIR", str(run["directory"].parent))
    monkeypatch.setenv("RUNLINE_GRAPHS_DIR", str(tmp_path / "no-graphs"))
    monkeypatch.setenv("RUNLINE_ALLOW_OSM_DOWNLOAD", "0")
    coverage._read_manifest.cache_clear()
    yield run
    coverage._read_manifest.cache_clear()


def _best_request(run) -> tuple[str, Coordinate, float]:
    """The pooled cell and distance with the most loops in one band."""

    def band(pool):
        distances = [loop["metrics"]["distance_miles"] for loop in pool["loops"]]
        return max((sum(abs(other - d) <= 0.25 for other in distances), d) for d in distances)

    cell = max(run["pools"], key=lambda cell: band(run["pools"][cell])[0])
    pool = run["pools"][cell]
    return cell, Coordinate(*pool["origin"]), round(band(pool)[1], 2)


def test_the_plan_lists_every_tile_disc_and_metro_label(run) -> None:
    plan = run["plan"]
    assert set(plan["tiles"]) == set(run["owner"].values())
    assert plan["labels"] == {"west-toy": "West Toy, VA", "east-toy": "East Toy, VA"}
    assert metro_label("New York--Jersey City--Newark, NY--NJ") == "New York, NY"
    assert metro_label("Urban Honolulu, HI") == "Urban Honolulu, HI"


def test_merge_indexes_every_planned_cell_by_its_tile(run) -> None:
    database = sqlite3.connect(run["directory"] / "pools.sqlite")
    cells = dict(database.execute("SELECT cell, tile FROM cells"))
    tiles = {row[0]: row[1:] for row in database.execute("SELECT tile, graph, metro, radius_meters FROM tiles")}
    meta = {key: json.loads(value) for key, value in database.execute("SELECT key, value FROM meta")}
    database.close()

    assert cells == run["owner"]
    assert set(run["pools"]) <= set(cells)
    database = sqlite3.connect(run["directory"] / "pools.sqlite")
    built_on = dict(database.execute("SELECT cell, tile FROM pools"))
    database.close()
    assert built_on == {cell: run["owner"][cell] for cell in run["pools"]}
    assert all(len(graph) == 16 and radius > 0 for graph, _, radius in tiles.values())
    assert {metro for _, metro, _ in tiles.values()} == {"west-toy", "east-toy"}
    assert meta["metro_index"] == merge_pools.METRO_INDEX_VERSION
    assert meta["metros"] == run["plan"]["labels"]


def test_every_planned_cell_resolves_to_its_owning_tile(served) -> None:
    for cell, tile in served["owner"].items():
        area = coverage.containing_area(cell_centre(cell))
        assert area is not None and area.slug == tile
        assert area.metro_pools and area.label in served["plan"]["labels"].values()


def test_the_api_serves_a_metro_pool(served) -> None:
    cell, origin, miles = _best_request(served)

    payload = asyncio.run(routes(RouteRequest(
        latitude=origin.latitude, longitude=origin.longitude, distance_miles=miles, surface="mixed", result_count=1,
    )))

    assert payload["source"] == "pool"
    assert "<gpx" in payload["routes"][0]["gpx"]
    # The loop is drawn on the owning tile's graph, where its node ids live.
    route = serving.serve_from_pool(origin, RoutePreferences(miles, surface=SurfacePreference.MIXED, result_count=1))
    graph = load_area_graph(coverage.containing_area(origin))
    assert graph.graph is not None and all(node in graph for node in route[0].node_ids)
    assert coverage.containing_area(origin).slug == served["owner"][cell]


def test_a_metro_start_with_no_pool_plans_live_on_its_tile(served, tmp_path) -> None:
    cell = next(cell for cell in sorted(served["owner"]) if cell not in served["pools"])
    preferences = RoutePreferences(1.5, surface=SurfacePreference.MIXED, result_count=1)

    result = plan_routes(cell_centre(cell), preferences, cache_directory=tmp_path, elevation_source="none")

    assert result.source == "live" and result.candidates


def test_areas_lists_the_served_metros_after_shipped_areas(run, monkeypatch) -> None:
    monkeypatch.setenv("RUNLINE_METROS_DIR", str(run["directory"].parent))
    coverage._read_manifest.cache_clear()

    labels = asyncio.run(supported_areas())["areas"]

    assert labels == ["Charlottesville, VA", "East Toy, VA", "West Toy, VA"]


def test_a_shipped_area_keeps_priority_over_a_metro(run, monkeypatch) -> None:
    monkeypatch.setenv("RUNLINE_METROS_DIR", str(run["directory"].parent))
    coverage._read_manifest.cache_clear()
    cell = next(iter(run["owner"]))

    assert coverage.containing_area(cell_centre(cell)).slug == "charlottesville-va"


def test_a_start_outside_every_metro_is_a_422(served) -> None:
    far = destination(Coordinate(*served["pools"][next(iter(served["pools"]))]["origin"]), 0, 20_000)

    with pytest.raises(HTTPException) as error:
        asyncio.run(routes(RouteRequest(latitude=far.latitude, longitude=far.longitude, distance_miles=2)))

    assert error.value.status_code == 422
    assert "no map data for that location" in error.value.detail
    assert "West Toy, VA" in error.value.detail


def _copy_run(run, tmp_path: Path, monkeypatch) -> Path:
    directory = tmp_path / "metros" / "copy"
    shutil.copytree(run["directory"], directory)
    monkeypatch.setenv("RUNLINE_METROS_DIR", str(directory.parent))
    return directory


def test_a_tile_graph_that_is_not_the_pools_graph_is_ignored(served, tmp_path, monkeypatch, caplog) -> None:
    directory = _copy_run(served, tmp_path, monkeypatch)
    cell, origin, miles = _best_request(served)
    tile = served["owner"][cell]
    other = next(slug for slug in served["plan"]["tiles"] if slug != tile)
    shutil.copy(directory / "tile-graphs" / f"{other}.pkl.gz", directory / "tile-graphs" / f"{tile}.pkl.gz")
    preferences = RoutePreferences(miles, surface=SurfacePreference.MIXED, result_count=1)

    with caplog.at_level(logging.WARNING, logger="runline.serving"):
        assert serving.serve_from_pool(origin, preferences) is None
    assert "built from graph" in caplog.text


def test_a_pool_built_on_another_tile_is_not_served(served, tmp_path, monkeypatch) -> None:
    directory = _copy_run(served, tmp_path, monkeypatch)
    cell, origin, miles = _best_request(served)
    database = sqlite3.connect(directory / "pools.sqlite")
    # Isolate this pool: valid neighboring cells can now answer the request.
    database.execute("DELETE FROM pools WHERE cell != ?", (cell,))
    database.execute("UPDATE pools SET tile = 'elsewhere' WHERE cell = ?", (cell,))
    database.commit()
    database.close()
    preferences = RoutePreferences(miles, surface=SurfacePreference.MIXED, result_count=1)

    assert serving.serve_from_pool(origin, preferences) is None


def test_a_missing_tile_graph_is_a_clear_422(served, tmp_path, monkeypatch) -> None:
    directory = _copy_run(served, tmp_path, monkeypatch)
    cell, origin, miles = _best_request(served)
    (directory / "tile-graphs" / f"{served['owner'][cell]}.pkl.gz").unlink()

    with pytest.raises(HTTPException) as error:
        asyncio.run(routes(RouteRequest(latitude=origin.latitude, longitude=origin.longitude, distance_miles=miles)))

    assert error.value.status_code == 422
    assert "missing from this deployment" in error.value.detail


def test_a_file_without_the_cell_index_is_not_served(served, tmp_path, monkeypatch, caplog) -> None:
    directory = _copy_run(served, tmp_path, monkeypatch)
    database = sqlite3.connect(directory / "pools.sqlite")
    database.execute("DELETE FROM meta WHERE key = 'metro_index'")
    database.commit()
    database.close()

    with caplog.at_level(logging.WARNING, logger="runline.metros"):
        assert metros.metro_runs() == ()
    assert "--add-index" in caplog.text


def _strip_index(path: Path) -> None:
    """Make a merged file look like one merged before the index (the DC pilot)."""

    database = sqlite3.connect(path)
    database.executescript(
        """
        DROP TABLE cells;
        DELETE FROM meta WHERE key IN ('metros', 'metro_index');
        CREATE TABLE old_tiles (tile TEXT PRIMARY KEY, graph TEXT) WITHOUT ROWID;
        INSERT INTO old_tiles SELECT tile, graph FROM tiles;
        DROP TABLE tiles;
        ALTER TABLE old_tiles RENAME TO tiles;
        ALTER TABLE pools DROP COLUMN tile;
        """
    )
    database.close()


def _index(path: Path) -> tuple:
    database = sqlite3.connect(path)
    try:
        return (
            sorted(database.execute("SELECT * FROM cells")),
            sorted(database.execute("SELECT cell, surface, tile FROM pools")),
            sorted(database.execute("SELECT tile, graph, metro, latitude, longitude, radius_meters FROM tiles")),
            sorted(database.execute("SELECT key, value FROM meta")),
        )
    finally:
        database.close()


def test_an_old_merge_gets_its_index_from_an_old_plan(run, tmp_path) -> None:
    merged = tmp_path / "pools.sqlite"
    shutil.copy(run["directory"] / "pools.sqlite", merged)
    _strip_index(merged)
    # A plan from before "tiles" and "labels": discs come from the config.
    old_plan = tmp_path / "plan.json.gz"
    with gzip.open(old_plan, "wt") as stream:
        json.dump({key: value for key, value in run["plan"].items() if key not in ("tiles", "labels")}, stream)

    assert merge_pools.main([
        "--plan", str(old_plan), "--add-index", str(merged), "--metros", str(run["root"] / "metros.json"),
    ]) == 0

    assert _index(merged) == _index(run["directory"] / "pools.sqlite")


def test_adding_an_index_from_the_wrong_plan_changes_nothing(run, tmp_path) -> None:
    merged = tmp_path / "pools.sqlite"
    shutil.copy(run["directory"] / "pools.sqlite", merged)
    _strip_index(merged)
    before = sqlite3.connect(merged).execute("SELECT sql FROM sqlite_master ORDER BY name").fetchall()
    wrong = tmp_path / "plan.json.gz"
    with gzip.open(wrong, "wt") as stream:
        json.dump({**run["plan"], "units": run["plan"]["units"][:1]}, stream)

    assert merge_pools.main(["--plan", str(wrong), "--add-index", str(merged)]) == 1

    assert sqlite3.connect(merged).execute("SELECT sql FROM sqlite_master ORDER BY name").fetchall() == before


def _rewrite_header(path: Path, **changes) -> None:
    with gzip.open(path, "rt") as stream:
        lines = stream.readlines()
    lines[0] = json.dumps({**json.loads(lines[0]), **changes}) + "\n"
    with gzip.open(path, "wt") as stream:
        stream.writelines(lines)


def test_merge_refuses_units_from_a_dirty_tree_among_clean_ones(run, tmp_path, capsys) -> None:
    shards = tmp_path / "shards"
    shutil.copytree(run["root"] / "shards", shards)
    first = sorted(shards.iterdir())[0]
    with gzip.open(first, "rt") as stream:
        dirty = json.loads(stream.readline())["dirty_src"]
    _rewrite_header(first, dirty_src=not dirty)

    assert merge_pools.main(["--plan", str(run["directory"] / "plan.json.gz"), "--input", str(shards)]) == 1
    assert "'dirty_src'" in capsys.readouterr().out
    assert not (shards / "pools.sqlite").exists()


def test_merge_refuses_a_tile_whose_units_were_built_on_different_graphs(run, tmp_path, capsys) -> None:
    shards = tmp_path / "shards"
    shutil.copytree(run["root"] / "shards", shards)
    # Split one unit in two, as a tile with more cells than a unit holds is.
    unit = run["plan"]["units"][0]
    halves = [
        {**unit, "id": f"{unit['id']}a", "cells": unit["cells"][:1]},
        {**unit, "id": f"{unit['id']}b", "cells": unit["cells"][1:]},
    ]
    plan = tmp_path / "plan.json.gz"
    with gzip.open(plan, "wt") as stream:
        json.dump({**run["plan"], "units": [*halves, *run["plan"]["units"][1:]]}, stream)
    original = shards / f"{unit['id']}.jsonl.gz"
    shutil.copy(original, shards / f"{unit['id']}a.jsonl.gz")
    original.rename(shards / f"{unit['id']}b.jsonl.gz")
    _rewrite_header(shards / f"{unit['id']}b.jsonl.gz", graph="0" * 16)

    assert merge_pools.main(["--plan", str(plan), "--input", str(shards)]) == 1
    assert "recompute the stale units" in capsys.readouterr().out


def test_tile_graphs_load_lazily_and_stay_within_the_cache(served, monkeypatch) -> None:
    monkeypatch.setenv("RUNLINE_TILE_GRAPHS_CACHE", "1")
    metros.TILE_GRAPHS.clear()
    first, second = (
        next(cell for cell, tile in served["owner"].items() if tile == slug)
        for slug in sorted(set(served["owner"].values()))[:2]
    )
    assert metros.TILE_GRAPHS.held() == 0

    one = load_area_graph(coverage.containing_area(cell_centre(first)))
    assert load_area_graph(coverage.containing_area(cell_centre(first))) is one
    load_area_graph(coverage.containing_area(cell_centre(second)))
    assert (metros.TILE_GRAPHS.held(), metros.TILE_GRAPHS.loads) == (1, 2)
    load_area_graph(coverage.containing_area(cell_centre(first)))
    assert metros.TILE_GRAPHS.loads == 3
    metros.TILE_GRAPHS.clear()


def test_warming_a_start_loads_its_tile_graph_ahead_of_the_request(served) -> None:
    cell, origin, _ = _best_request(served)
    metros.TILE_GRAPHS.clear()

    assert serving.warm(origin) in served["plan"]["labels"].values()
    assert metros.TILE_GRAPHS.loads == 1
    # The route request that follows reuses the warmed graph.
    load_area_graph(coverage.containing_area(origin))
    assert metros.TILE_GRAPHS.loads == 1


def test_the_warm_endpoint_names_the_area_and_loads_after_responding(served) -> None:
    from fastapi import BackgroundTasks

    from runline.api import WarmRequest, warm_start

    _, origin, _ = _best_request(served)
    metros.TILE_GRAPHS.clear()
    tasks = BackgroundTasks()
    payload = asyncio.run(warm_start(WarmRequest(latitude=origin.latitude, longitude=origin.longitude), tasks))

    assert payload["area"] in served["plan"]["labels"].values()
    assert metros.TILE_GRAPHS.loads == 0  # nothing loads before the response
    asyncio.run(tasks())
    assert metros.TILE_GRAPHS.loads == 1


def test_reading_a_graph_leaves_the_garbage_collector_as_it_was(run) -> None:
    import gc

    path = next((run["directory"] / metros.TILE_GRAPHS_NAME).glob("*.pkl.gz"))
    assert gc.isenabled()
    metros.read_graph_file(path)
    assert gc.isenabled()
    gc.disable()
    try:
        metros.read_graph_file(path)
        assert not gc.isenabled()
    finally:
        gc.enable()


# On-demand tile graph fetching (runline.tile_fetch): the run ships without
# its tile graphs and a local HTTP server stands in for object storage.


class _Bucket:
    """A loopback HTTP server over a directory, counting requests per path."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.requests: Counter[str] = Counter()
        self.delay = 0.0
        self.status = 200
        bucket = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 (http.server's name)
                bucket.requests[self.path] += 1
                time.sleep(bucket.delay)
                path = bucket.root / self.path.lstrip("/")
                if bucket.status != 200 or not path.is_file():
                    self.send_error(bucket.status if bucket.status != 200 else 404)
                    return
                body = path.read_bytes()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args) -> None:
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def remote(served, tmp_path, monkeypatch):
    """A copy of the run whose tile graphs live only in the bucket."""

    directory = _copy_run(served, tmp_path, monkeypatch)
    bucket_root = tmp_path / "bucket"
    (bucket_root / directory.name).mkdir(parents=True)
    shutil.move(directory / "tile-graphs", bucket_root / directory.name / "tile-graphs")
    bucket = _Bucket(bucket_root)
    monkeypatch.setenv("RUNLINE_TILE_GRAPHS_URL", bucket.url)
    monkeypatch.setenv("RUNLINE_TILE_DISK_CACHE_DIR", str(tmp_path / "tile-cache"))
    monkeypatch.setenv("RUNLINE_TILE_FETCH_TIMEOUT", "5")
    monkeypatch.setattr(metros, "FETCHER", tile_fetch.TileGraphFetcher())
    metros.TILE_GRAPHS.clear()
    yield {**served, "directory": directory, "bucket": bucket, "cache": tmp_path / "tile-cache"}
    bucket.close()
    metros.TILE_GRAPHS.clear()


def _tile_cells(run) -> list[str]:
    """One cell in each tile, in tile order."""

    return [next(cell for cell, tile in sorted(run["owner"].items()) if tile == slug) for slug in sorted(set(run["owner"].values()))]


def _cached(remote) -> list[str]:
    return sorted(path.name for path in remote["cache"].glob("*.pkl.gz"))


def test_a_missing_tile_graph_is_fetched_verified_and_served_from_its_pool(remote) -> None:
    cell, origin, miles = _best_request(remote)
    request = RouteRequest(
        latitude=origin.latitude, longitude=origin.longitude, distance_miles=miles, surface="mixed", result_count=1,
    )

    assert asyncio.run(routes(request))["source"] == "pool"
    assert asyncio.run(routes(request))["source"] == "pool"

    tile = remote["owner"][cell]
    expected = metros.tile_for(coverage.containing_area(origin)).graph
    assert _cached(remote) == [f"{tile}-{expected}.pkl.gz"]
    assert metros.FETCHER.downloads == 1
    assert remote["bucket"].requests == {f"/copy/tile-graphs/{tile}.pkl.gz": 1}
    assert not (remote["directory"] / "tile-graphs").exists()


def test_a_fetched_graph_that_is_not_the_pools_graph_is_never_loaded(remote, monkeypatch) -> None:
    cell, origin, miles = _best_request(remote)
    tile = remote["owner"][cell]
    other = next(slug for slug in remote["plan"]["tiles"] if slug != tile)
    graphs = remote["bucket"].root / "copy" / "tile-graphs"
    shutil.copy(graphs / f"{other}.pkl.gz", graphs / f"{tile}.pkl.gz")
    monkeypatch.setattr(metros, "read_graph_file", lambda path: pytest.fail(f"loaded unverified {path}"))

    for _ in range(2):
        with pytest.raises(HTTPException) as error:
            asyncio.run(routes(RouteRequest(latitude=origin.latitude, longitude=origin.longitude, distance_miles=miles)))
        assert error.value.status_code == 503
        assert "temporarily unavailable" in error.value.detail

    assert _cached(remote) == [] and not list(remote["cache"].glob("*.part"))
    # The failure is remembered briefly instead of refetched by each request.
    assert sum(remote["bucket"].requests.values()) == 1


def test_a_corrupted_cached_graph_is_fetched_again(remote) -> None:
    cell = _tile_cells(remote)[0]
    area = coverage.containing_area(cell_centre(cell))
    load_area_graph(area)
    metros.TILE_GRAPHS.clear()
    (cached,) = remote["cache"].glob("*.pkl.gz")
    cached.write_bytes(b"not a graph")

    load_area_graph(area)

    assert metros.FETCHER.downloads == 2


@pytest.mark.parametrize("failure", ["missing", "server error", "timeout"])
def test_a_failed_fetch_is_a_503_not_a_500(remote, monkeypatch, failure) -> None:
    _, origin, miles = _best_request(remote)
    if failure == "missing":
        shutil.rmtree(remote["bucket"].root / "copy")
    elif failure == "server error":
        remote["bucket"].status = 500
    else:
        monkeypatch.setenv("RUNLINE_TILE_FETCH_TIMEOUT", "0.2")
        remote["bucket"].delay = 1.0

    with pytest.raises(HTTPException) as error:
        asyncio.run(routes(RouteRequest(latitude=origin.latitude, longitude=origin.longitude, distance_miles=miles)))

    assert error.value.status_code == 503
    assert "temporarily unavailable" in error.value.detail
    assert _cached(remote) == []


def test_concurrent_requests_for_a_cold_tile_download_it_once(remote) -> None:
    remote["bucket"].delay = 0.3
    area = coverage.containing_area(cell_centre(_tile_cells(remote)[0]))
    barrier = threading.Barrier(4)

    def load(_):
        barrier.wait()
        return load_area_graph(area)

    with ThreadPoolExecutor(4) as pool:
        graphs = list(pool.map(load, range(4)))

    assert all(graph is graphs[0] for graph in graphs)
    assert metros.FETCHER.downloads == 1 and sum(remote["bucket"].requests.values()) == 1
    assert metros.TILE_GRAPHS.loads == 1


def test_the_disk_cache_evicts_graphs_no_longer_in_memory(remote, monkeypatch) -> None:
    first, second = (coverage.containing_area(cell_centre(cell)) for cell in _tile_cells(remote)[:2])
    sizes = [path.stat().st_size for path in (remote["bucket"].root / "copy" / "tile-graphs").iterdir()]
    # Room for one graph, not two.
    monkeypatch.setenv("RUNLINE_TILE_DISK_CACHE_GB", str(max(sizes) * 1.5 / 1e9))
    name = lambda area: f"{area.slug}-{metros.tile_for(area).graph}.pkl.gz"  # noqa: E731

    monkeypatch.setenv("RUNLINE_TILE_GRAPHS_CACHE", "2")
    load_area_graph(first)
    load_area_graph(second)
    # Both graphs are in memory, so the cache stays over its budget.
    assert _cached(remote) == sorted([name(first), name(second)])

    shutil.rmtree(remote["cache"])
    metros.TILE_GRAPHS.clear()
    monkeypatch.setenv("RUNLINE_TILE_GRAPHS_CACHE", "1")
    load_area_graph(first)
    # Loading the second pushes the first out of memory, then off the disk.
    load_area_graph(second)

    assert _cached(remote) == [name(second)]
    assert metros.FETCHER.downloads == 4


def test_eviction_takes_the_least_recently_used_unprotected_files(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("RUNLINE_TILE_DISK_CACHE_DIR", str(tmp_path))
    monkeypatch.setenv("RUNLINE_TILE_DISK_CACHE_GB", str(2_500 / 1e9))
    for age, name in enumerate(["newest", "middle", "oldest", "held"]):
        path = tmp_path / f"{name}-{'0' * 16}.pkl.gz"
        path.write_bytes(b"x" * 1_000)
        used = time.time_ns() - (age + 1) * 10**9
        os.utime(path, ns=(used, used))
    (tmp_path / ".stale.pkl.gz.part").write_bytes(b"x")
    os.utime(tmp_path / ".stale.pkl.gz.part", (0, 0))

    tile_fetch.TileGraphFetcher().evict({str(tmp_path / f"held-{'0' * 16}.pkl.gz")})

    assert sorted(path.name.split("-")[0] for path in tmp_path.iterdir()) == ["held", "newest"]


def test_tile_urls_need_https_and_follow_the_run_layout(tmp_path, monkeypatch) -> None:
    assert tile_fetch.tile_url("https://bucket.example/runs/", "b1", "nyc-0001", "ab" * 8) == (
        "https://bucket.example/runs/b1/tile-graphs/nyc-0001.pkl.gz"
    )
    assert tile_fetch.tile_url("https://cdn.example/{graph}/{tile}.gz", "b1", "t", "ab" * 8) == (
        f"https://cdn.example/{'ab' * 8}/t.gz"
    )
    monkeypatch.setenv("RUNLINE_TILE_GRAPHS_URL", "http://bucket.example")
    monkeypatch.setenv("RUNLINE_TILE_DISK_CACHE_DIR", str(tmp_path))
    opened = []
    fetcher = tile_fetch.TileGraphFetcher(opener=lambda *args, **kwargs: opened.append(args))

    with pytest.raises(coverage.MapDataUnavailable):
        with fetcher.use("b1", "t", "ab" * 8, "Toy"):
            pass

    assert opened == [] and list(tmp_path.glob("*.pkl.gz")) == []


# Start -> finish on tile graphs. The toy tiles are 3.8 km discs whose centres
# sit 1.3-1.7 km apart on one line, so most points lie in several discs.


def _discs(run) -> dict[str, tuple[Coordinate, float]]:
    return {
        slug: (Coordinate(tile["latitude"], tile["longitude"]), tile["radius_meters"])
        for slug, tile in run["plan"]["tiles"].items()
    }


def _holding(run, *points: Coordinate) -> set[str]:
    return {
        slug for slug, (centre, radius) in _discs(run).items()
        if all(distance_meters(centre, point) <= radius for point in points)
    }


def _tile_graph(run, slug: str) -> nx.MultiDiGraph:
    return metros.read_graph_file(run["directory"] / "tile-graphs" / f"{slug}.pkl.gz")


def _where(graph, node) -> Coordinate:
    return Coordinate(graph.nodes[node]["y"], graph.nodes[node]["x"])


def _owned_nodes(run, slug: str) -> list[int]:
    """Nodes of a tile's graph inside cells that tile owns, so a start there resolves to it."""

    graph = _tile_graph(run, slug)
    grid = run["plan"]["grid_meters"]
    return [
        node for node in sorted(graph.nodes)
        if run["owner"].get(cell_id(graph.nodes[node]["y"], graph.nodes[node]["x"], grid)) == slug
    ]


def _reachable_finish(graph, start, accept) -> int:
    reachable = nx.descendants(graph, start)
    return next(node for node in sorted(reachable) if accept(_where(graph, node)))


@pytest.fixture
def used_tiles(served, monkeypatch) -> list[str]:
    """The tile slugs direct planning loaded a graph for, in order."""

    used = []

    def load(area):
        used.append(area.slug)
        return load_area_graph(area)

    monkeypatch.setattr(direct, "load_area_graph", load)
    metros.TILE_GRAPHS.clear()
    yield used
    metros.TILE_GRAPHS.clear()


def _direct(start: Coordinate, finish: Coordinate) -> dict:
    return asyncio.run(routes(RouteRequest(
        mode="point_to_point", latitude=start.latitude, longitude=start.longitude,
        destination={"latitude": finish.latitude, "longitude": finish.longitude},
    )))


def test_a_direct_route_runs_on_the_starts_own_tile(served, used_tiles) -> None:
    tile = "west-toy-001"
    graph = _tile_graph(served, tile)
    start_node = _owned_nodes(served, tile)[0]
    start = _where(graph, start_node)
    finish_node = _reachable_finish(graph, start_node, lambda point: 800 <= distance_meters(start, point) <= 1_500)
    finish = _where(graph, finish_node)
    # Both endpoints sit in the overlap of several tiles; the owning one wins.
    assert tile in _holding(served, start, finish) and len(_holding(served, start, finish)) >= 2

    payload = _direct(start, finish)

    assert used_tiles == [tile] and metros.TILE_GRAPHS.loads == 1
    assert payload["mode"] == "point_to_point" and payload["source"] == "live"
    assert payload["origin"] == {"latitude": start.latitude, "longitude": start.longitude}
    assert payload["destination"] == {"latitude": finish.latitude, "longitude": finish.longitude}
    line = payload["routes"][0]["feature"]["geometry"]["coordinates"]
    assert line[0] != line[-1]
    assert payload["routes"][0]["gpx"].count("<trkpt") == len(line)


def test_a_finish_beyond_the_owning_tile_uses_an_overlapping_tile_holding_both(served, used_tiles) -> None:
    owner, other = "west-toy-001", "west-toy-000"
    start_node = _owned_nodes(served, owner)[0]
    start = _where(_tile_graph(served, owner), start_node)
    centre, radius = _discs(served)[owner]
    graph = _tile_graph(served, other)
    other_centre, other_radius = _discs(served)[other]
    finish = _where(graph, _reachable_finish(
        graph, start_node,
        lambda point: distance_meters(centre, point) > radius + 100
        and distance_meters(other_centre, point) < other_radius - 100,
    ))
    assert owner not in _holding(served, start, finish) and other in _holding(served, start, finish)

    result = direct.plan_point_to_point(start, finish, RoutePreferences(1), cache_directory=Path("unused"))

    assert used_tiles == [other]
    assert result.candidates[0].coordinates[0] == start
    assert result.candidates[0].coordinates[-1] == finish
    assert all(node in graph for node in result.candidates[0].node_ids)


def test_endpoints_with_no_common_tile_are_an_explained_422(served, used_tiles) -> None:
    graph = _tile_graph(served, "west-toy-000")
    start = min((_where(graph, node) for node in _owned_nodes(served, "west-toy-000")), key=lambda point: point.longitude)
    holding_start = _holding(served, start)
    east = _tile_graph(served, "east-toy-001")
    east_centre, east_radius = _discs(served)["east-toy-001"]
    finish = next(
        _where(east, node) for node in sorted(east.nodes)
        if distance_meters(east_centre, _where(east, node)) < east_radius - 100
        and all(distance_meters(_discs(served)[slug][0], _where(east, node)) > _discs(served)[slug][1]
                for slug in holding_start)
    )
    assert not _holding(served, start, finish) and _holding(served, finish)

    with pytest.raises(HTTPException) as error:
        _direct(start, finish)

    assert error.value.status_code == 422
    assert "same supported area" in error.value.detail and "one map tile" in error.value.detail
    assert used_tiles == []


def test_a_direct_request_outside_every_metro_is_an_explained_422(served, used_tiles, monkeypatch) -> None:
    monkeypatch.delenv("RUNLINE_REMOTE_API", raising=False)
    origin = Coordinate(*served["pools"][next(iter(served["pools"]))]["origin"])
    far, farther = destination(origin, 0, 20_000), destination(origin, 0, 21_000)

    with pytest.raises(HTTPException) as error:
        _direct(far, farther)

    assert error.value.status_code == 422
    assert "same supported area" in error.value.detail
    assert "Available direct-route coverage:" in error.value.detail and "West Toy, VA" in error.value.detail
    assert "one map tile" not in error.value.detail and used_tiles == []


def test_direct_planning_keeps_within_the_tile_graph_cache(served, used_tiles, monkeypatch) -> None:
    monkeypatch.setenv("RUNLINE_TILE_GRAPHS_CACHE", "1")
    for tile in ("west-toy-001", "east-toy-001"):
        graph = _tile_graph(served, tile)
        start_node = _owned_nodes(served, tile)[0]
        start = _where(graph, start_node)
        finish = _where(graph, _reachable_finish(graph, start_node, lambda point: distance_meters(start, point) >= 500))
        direct.plan_point_to_point(start, finish, RoutePreferences(1), cache_directory=Path("unused"))
        assert metros.TILE_GRAPHS.held() == 1
    assert used_tiles == ["west-toy-001", "east-toy-001"]


def test_capabilities_report_direct_metros_only_with_their_tile_graphs_here(served, tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("RUNLINE_REMOTE_API", raising=False)
    modes = asyncio.run(capabilities())["modes"]
    assert sorted(modes["point_to_point"]["areas"]) == ["East Toy, VA", "West Toy, VA"]

    directory = _copy_run(served, tmp_path, monkeypatch)
    (directory / "tile-graphs" / "west-toy-000.pkl.gz").unlink()
    modes = asyncio.run(capabilities())["modes"]
    assert modes["point_to_point"]["areas"] == ["East Toy, VA"]
    # Loops still list both: their coverage is the pools' index.
    assert sorted(modes["loop"]["areas"]) == ["East Toy, VA", "West Toy, VA"]

    for path in (directory / "tile-graphs").iterdir():
        path.unlink()
    assert asyncio.run(capabilities())["modes"]["point_to_point"]["areas"] == []
    start = cell_centre(next(iter(served["owner"])))
    with pytest.raises(CoverageError, match="missing from this deployment"):
        direct.plan_point_to_point(start, destination(start, 90, 500), RoutePreferences(1), cache_directory=tmp_path)


def test_a_shipped_area_keeps_priority_for_direct_routes(run, monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("RUNLINE_METROS_DIR", str(run["directory"].parent))
    coverage._read_manifest.cache_clear()
    used = []
    monkeypatch.setattr(direct, "load_area_graph", lambda area: used.append(area.slug) or load_area_graph(area))
    tile = "east-toy-000"
    graph = _tile_graph(run, tile)
    start_node = _owned_nodes(run, tile)[0]
    start = _where(graph, start_node)
    finish = _where(graph, _reachable_finish(graph, start_node, lambda point: distance_meters(start, point) >= 500))

    direct.plan_point_to_point(start, finish, RoutePreferences(1), cache_directory=tmp_path)

    assert used == ["charlottesville-va"]
    assert sorted(direct.direct_area_labels()) == ["Charlottesville, VA", "East Toy, VA", "West Toy, VA"]


# Compact tile graphs (runline.compact) beside or instead of the pickles.


@pytest.fixture(scope="module")
def compacted(run, tmp_path_factory) -> Path:
    """The run with its tile graphs converted in place, as on the VM."""

    from scripts import compact_graphs

    directory = tmp_path_factory.mktemp("compacted") / "run"
    shutil.copytree(run["directory"], directory)
    assert compact_graphs.main([str(directory / "tile-graphs"), "--verify"]) == 0
    return directory


def _compact_run(compacted: Path, tmp_path: Path, monkeypatch, *, keep_pickles: bool = False) -> Path:
    directory = tmp_path / "metros" / "compact"
    shutil.copytree(compacted, directory)
    monkeypatch.setenv("RUNLINE_METROS_DIR", str(directory.parent))
    graphs = directory / "tile-graphs"
    if not keep_pickles:
        for path in graphs.glob("*.pkl.gz"):
            path.unlink()
    metros.TILE_GRAPHS.clear()
    metros.TILE_ARRAYS.clear()
    serving._tile_graph_matches.cache_clear()
    return directory


def _served_routes(origin: Coordinate, miles: float, count: int = 1) -> list:
    preferences = RoutePreferences(miles, surface=SurfacePreference.MIXED, result_count=count)
    routes_found = serving.serve_from_pool(origin, preferences)
    return routes_found and [dataclasses.asdict(route) for route in routes_found]


@pytest.mark.parametrize("arrays", [True, False])
@pytest.mark.parametrize("keep_pickles", [False, True])
def test_pools_built_on_pickles_serve_identically_from_compact_tiles(
    served, compacted, tmp_path, monkeypatch, keep_pickles, arrays
) -> None:
    from runline.compact import COMPACT_SUFFIX

    cell, origin, miles = _best_request(served)
    metros.TILE_GRAPHS.clear()
    from_pickles = _served_routes(origin, miles)
    assert from_pickles

    monkeypatch.setenv("RUNLINE_ARRAY_SERVING", "1" if arrays else "0")
    directory = _compact_run(compacted, tmp_path, monkeypatch, keep_pickles=keep_pickles)
    from_compact = _served_routes(origin, miles)

    assert from_compact == from_pickles
    # A hit on a compact tile holds its arrays, not a networkx graph.
    held = metros.TILE_ARRAYS._graphs if arrays else metros.TILE_GRAPHS._graphs
    assert held and all(path.endswith(COMPACT_SUFFIX) for path, _ in held)
    assert (metros.TILE_GRAPHS.held(), metros.TILE_ARRAYS.held()) == ((0, 1) if arrays else (1, 0))
    payload = asyncio.run(routes(RouteRequest(
        latitude=origin.latitude, longitude=origin.longitude, distance_miles=miles, surface="mixed", result_count=1,
    )))
    assert payload["source"] == "pool"
    assert (directory / "tile-graphs" / f"{served['owner'][cell]}.pkl.gz").exists() == keep_pickles
    metros.TILE_GRAPHS.clear()
    metros.TILE_ARRAYS.clear()


def test_every_pooled_request_serves_the_same_from_arrays_and_graphs(served, compacted, tmp_path, monkeypatch) -> None:
    _compact_run(compacted, tmp_path, monkeypatch)
    requests = [
        (Coordinate(*pool["origin"]), round(loop["metrics"]["distance_miles"], 1), count)
        for pool in served["pools"].values()
        for loop in pool["loops"]
        for count in (1, 3)
    ]
    answers = {}
    for arrays in ("1", "0"):
        monkeypatch.setenv("RUNLINE_ARRAY_SERVING", arrays)
        answers[arrays] = [
            _served_routes(origin, miles, count) for origin, miles, count in requests
        ]
    assert any(answers["1"])
    assert answers["1"] == answers["0"]
    metros.TILE_GRAPHS.clear()
    metros.TILE_ARRAYS.clear()


def test_a_live_request_on_a_compact_tile_builds_its_graph_from_the_arrays(served, compacted, tmp_path, monkeypatch) -> None:
    from runline import osm

    cell = next(cell for cell in sorted(served["owner"]) if cell not in served["pools"])
    preferences = RoutePreferences(1.5, surface=SurfacePreference.MIXED, result_count=1)
    osm._PREPARED_CACHE.clear()
    expected = plan_routes(cell_centre(cell), preferences, cache_directory=tmp_path, elevation_source="none")
    _compact_run(compacted, tmp_path, monkeypatch)
    osm._PREPARED_CACHE.clear()
    path = metros.graph_file(coverage.containing_area(cell_centre(cell)).filename)
    tile = metros.TILE_ARRAYS.get(path)

    result = plan_routes(cell_centre(cell), preferences, cache_directory=tmp_path, elevation_source="none")

    assert result.source == "live" and result.candidates
    assert metros.TILE_GRAPHS.held() == 1 and metros.TILE_ARRAYS.peek(path) is tile
    assert [dataclasses.asdict(c) for c in result.candidates] == [dataclasses.asdict(c) for c in expected.candidates]
    osm._PREPARED_CACHE.clear()
    metros.TILE_GRAPHS.clear()
    metros.TILE_ARRAYS.clear()


def test_a_tampered_compact_tile_is_refused(served, compacted, tmp_path, monkeypatch, caplog) -> None:
    cell, origin, miles = _best_request(served)
    directory = _compact_run(compacted, tmp_path, monkeypatch)
    path = directory / "tile-graphs" / f"{served['owner'][cell]}.npz"
    with np.load(path, allow_pickle=False) as archive:
        arrays = {name: archive[name] for name in archive.files}
    arrays["edge_length"] = arrays["edge_length"] * 1.0001
    with path.open("wb") as handle:
        np.savez_compressed(handle, **arrays)

    with caplog.at_level(logging.WARNING):
        assert _served_routes(origin, miles) is None
    assert "built from graph" in caplog.text
    with pytest.raises(HTTPException) as error:
        asyncio.run(routes(RouteRequest(latitude=origin.latitude, longitude=origin.longitude, distance_miles=miles)))
    assert error.value.status_code == 422 and "damaged" in error.value.detail
    metros.TILE_GRAPHS.clear()


def test_a_compact_tile_of_another_graph_is_refused(served, compacted, tmp_path, monkeypatch, caplog) -> None:
    cell, origin, miles = _best_request(served)
    directory = _compact_run(compacted, tmp_path, monkeypatch)
    tile = served["owner"][cell]
    other = next(slug for slug in served["plan"]["tiles"] if slug != tile)
    shutil.copy(directory / "tile-graphs" / f"{other}.npz", directory / "tile-graphs" / f"{tile}.npz")

    with caplog.at_level(logging.WARNING, logger="runline.serving"):
        assert _served_routes(origin, miles) is None
    assert "built from graph" in caplog.text
    metros.TILE_GRAPHS.clear()


@pytest.mark.parametrize("arrays", [True, False])
def test_warming_a_compact_start_loads_what_its_first_pool_hit_reads(
    served, compacted, tmp_path, monkeypatch, arrays
) -> None:
    monkeypatch.setenv("RUNLINE_ARRAY_SERVING", "1" if arrays else "0")
    _compact_run(compacted, tmp_path, monkeypatch)
    _, origin, miles = _best_request(served)
    warmed, untouched = (metros.TILE_ARRAYS, metros.TILE_GRAPHS) if arrays else (metros.TILE_GRAPHS, metros.TILE_ARRAYS)

    assert serving.warm(origin) in served["plan"]["labels"].values()
    assert (warmed.loads, untouched.loads) == (1, 0)
    # The pool hit that follows reuses what was warmed and loads nothing else.
    assert _served_routes(origin, miles)
    assert (warmed.loads, untouched.loads) == (1, 0)
    metros.TILE_GRAPHS.clear()
    metros.TILE_ARRAYS.clear()


def test_every_planned_cell_serves_the_same_from_arrays_and_graphs_retries_included(
    served, compacted, tmp_path, monkeypatch, caplog
) -> None:
    """Misses too, so the connected-start and wider-ring retries run on both."""

    from runline.array_tiles import ArrayTile

    _compact_run(compacted, tmp_path, monkeypatch)
    starts = [cell_centre(cell) for cell in sorted(served["owner"])]
    caplog.set_level("DEBUG", logger="runline.serving")

    def answers() -> tuple[list, list]:
        caplog.clear()
        found = [_served_routes(origin, miles) for origin in starts for miles in (2.0, 3.0)]
        return found, [record.pool_retry for record in caplog.records if hasattr(record, "pool_retry")]

    monkeypatch.setenv("RUNLINE_ARRAY_SERVING", "0")
    from_graphs, graph_retries = answers()
    monkeypatch.setenv("RUNLINE_ARRAY_SERVING", "1")
    metros.TILE_ARRAYS.clear()
    from_arrays, array_retries = answers()
    # A tie for the largest component sends the retry to the networkx graph.
    metros.TILE_ARRAYS.clear()
    monkeypatch.setattr(ArrayTile, "core_mask", lambda self: None)
    from_fallback, fallback_retries = answers()

    assert any(from_graphs) and not all(from_graphs)
    assert graph_retries, "no request was answered on a retry"
    assert from_arrays == from_graphs and array_retries == graph_retries
    assert from_fallback == from_graphs and fallback_retries == graph_retries
    metros.TILE_GRAPHS.clear()
    metros.TILE_ARRAYS.clear()


def _shards_with_one_rerun(run, tmp_path) -> tuple[Path, Path, str]:
    """A copy of the run's shards whose last unit says it was rebuilt at another commit."""

    shards = tmp_path / "shards"
    shutil.copytree(run["root"] / "shards", shards)
    last = sorted(shards.glob("*.jsonl.gz"))[-1]
    _rewrite_header(last, commit="f00dbee")
    return shards, run["directory"] / "plan.json.gz", "f00dbee"


def test_merge_refuses_a_unit_rerun_at_another_commit_unless_allowed(run, tmp_path, capsys) -> None:
    shards, plan, rerun = _shards_with_one_rerun(run, tmp_path)

    assert merge_pools.main(["--plan", str(plan), "--input", str(shards)]) == 1
    assert "'commit'" in capsys.readouterr().out
    assert merge_pools.main(["--plan", str(plan), "--input", str(shards), "--allow-commit", rerun]) == 0

    database = sqlite3.connect(shards / "pools.sqlite")
    meta = {key: json.loads(value) for key, value in database.execute("SELECT key, value FROM meta")}
    database.close()
    assert rerun in meta["commits"] and len(meta["commits"]) == 2
    assert meta["commit"] in meta["commits"]


def test_an_allowed_commit_does_not_excuse_other_differences(run, tmp_path, capsys) -> None:
    shards, plan, rerun = _shards_with_one_rerun(run, tmp_path)
    last = sorted(shards.glob("*.jsonl.gz"))[-1]
    _rewrite_header(last, targets=[1, 2])

    assert merge_pools.main(["--plan", str(plan), "--input", str(shards), "--allow-commit", rerun]) == 1
    assert "'targets'" in capsys.readouterr().out
    assert not (shards / "pools.sqlite").exists()


def test_allowing_one_commit_does_not_admit_a_third(run, tmp_path, capsys) -> None:
    shards, plan, rerun = _shards_with_one_rerun(run, tmp_path)
    _rewrite_header(sorted(shards.glob("*.jsonl.gz"))[-2], commit="0ther00")

    assert merge_pools.main(["--plan", str(plan), "--input", str(shards), "--allow-commit", rerun]) == 1


def test_an_allowed_commit_may_come_first(run, tmp_path) -> None:
    shards = tmp_path / "shards"
    shutil.copytree(run["root"] / "shards", shards)
    _rewrite_header(sorted(shards.glob("*.jsonl.gz"))[0], commit="f00dbee")

    assert merge_pools.main(["--plan", str(run["directory"] / "plan.json.gz"), "--input", str(shards),
                             "--allow-commit", "f00dbee"]) == 0
