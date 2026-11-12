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

import networkx as nx

from runline import coverage, metros, serving
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
