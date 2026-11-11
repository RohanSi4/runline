"""Serving a metro run end to end: plan tiles, build tile graphs, precompute, merge, serve.

Two toy metros sit either side of the Charlottesville map centre, with tiles
sized for 3 mile loops so each tile graph is a small crop of the shipped
graph. Every test runs with no shipped areas, so a start can only be served
through its metro tile.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import logging
import math
import pickle
import shutil
import sqlite3
from pathlib import Path

import numpy as np
import pytest
from fastapi import HTTPException
from shapely import affinity
from shapely.geometry import Point, mapping

from runline import coverage, metros, serving
from runline.api import RouteRequest, routes, supported_areas
from runline.coverage import areas, load_area_graph
from runline.geo import destination
from runline.grid import cell_centre
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
        """
    )
    database.close()


def _index(path: Path) -> tuple:
    database = sqlite3.connect(path)
    try:
        return (
            sorted(database.execute("SELECT * FROM cells")),
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
