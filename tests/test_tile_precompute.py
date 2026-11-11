"""Metro runs: every cell computed once, on its own tile's graph."""

from __future__ import annotations

import gzip
import json
import math
import shutil
import sqlite3
from pathlib import Path

import numpy as np
import pytest
from shapely import affinity
from shapely.geometry import Point

from runline.coverage import areas, load_area_graph
from runline.geo import distance_meters
from runline.grid import cell_centre
from runline.models import Coordinate
from runline.pools import decode_pool
from runline.tiling import metro_cells, owned_cells, plan_tiles
from scripts import merge_pools, plan_tile_pools, precompute_pools


def _ellipse(latitude: float, longitude: float, width_km: float, height_km: float):
    circle = Point(longitude, latitude).buffer(1.0, quad_segs=32)
    return affinity.scale(
        circle,
        width_km / 2 / (111.32 * math.cos(math.radians(latitude))),
        height_km / 2 / 110.54,
    )


def _config(shapes: dict) -> dict:
    metros = []
    for slug, shape in shapes.items():
        plan = plan_tiles(shape, slug)
        counts = np.bincount(plan.owner, minlength=len(plan.tiles))
        metros.append(
            {
                "slug": slug,
                "tiles": [tile.to_json(slug, slug, int(count)) for tile, count in zip(plan.tiles, counts)],
            }
        )
    return {"grid_meters": 200.0, "max_miles": 12.0, "metros": metros}


def test_overlapping_metros_share_no_cell() -> None:
    # Two metros 25 km apart whose boundaries overlap in the middle.
    shapes = {"west": _ellipse(10.0, 20.0, 40, 12), "east": _ellipse(10.0, 20.23, 40, 12)}
    config = _config(shapes)

    owned = owned_cells(config, shapes)

    every = [cell for cells in owned.values() for cell in cells]
    assert len(every) == len(set(every))
    expected = set(metro_cells(shapes["west"]).ids()) | set(metro_cells(shapes["east"]).ids())
    assert set(every) == expected
    tiles = {tile["slug"]: tile for metro in config["metros"] for tile in metro["tiles"]}
    for slug, cells in owned.items():
        tile = tiles[slug]
        centre = Coordinate(tile["latitude"], tile["longitude"])
        for cell in cells[::25]:
            assert distance_meters(centre, cell_centre(cell)) <= tile["reach_meters"] + 1e-6


def test_units_split_tiles_and_tasks_cover_every_unit_once() -> None:
    owned = {"b-001": [f"200:1:{index}" for index in range(250)], "a-000": ["200:0:0"], "c-002": []}

    units = plan_tile_pools.plan_units(owned, 100)

    assert [unit["id"] for unit in units] == ["a-000-p00", "b-001-p00", "b-001-p01", "b-001-p02"]
    assert [len(unit["cells"]) for unit in units] == [1, 100, 100, 50]
    for count in (1, 3, 4, 7):
        blocks = [precompute_pools.task_units(units, index, count) for index in range(count)]
        assert [unit for block in blocks for unit in block] == units


def _disc(centre: Coordinate, radius_meters: float) -> dict:
    return {"latitude": centre.latitude, "longitude": centre.longitude, "radius_meters": radius_meters}


@pytest.fixture(scope="module")
def tile_run(tmp_path_factory) -> dict:
    """A plan with the shipped Charlottesville graph standing in for a tile."""

    root = tmp_path_factory.mktemp("tiles")
    area = next(item for item in areas() if item.slug == "charlottesville-va")
    graph = load_area_graph(area)
    cells = precompute_pools.area_cells(area, graph, 200.0)[::1500][:2]
    graphs = root / "graphs"
    graphs.mkdir()
    shutil.copy(Path(precompute_pools.ROOT) / "graphs" / area.filename, graphs / "cville-000.pkl.gz")
    # An all-water tile: build_tile_graphs leaves stats and no graph.
    (graphs / "sea-000.json").write_text(json.dumps({"slug": "sea-000", "nodes": 0, "empty": True}))
    plan = {
        "version": 1,
        "grid_meters": 200.0,
        "max_miles": 12.0,
        "metros": ["test"],
        "units": [
            # 200:0:0 is in the Gulf of Guinea, so no node of the tile is in it.
            {"id": "cville-000-p00", "tile": "cville-000", "cells": [*cells, "200:0:0"]},
            {"id": "sea-000-p00", "tile": "sea-000", "cells": ["200:0:1"]},
        ],
        "tiles": {
            "cville-000": {"metro": "test", **_disc(area.center, area.radius_meters)},
            "sea-000": {"metro": "test", **_disc(Coordinate(0.001, 0.003), 1000.0)},
        },
        "labels": {"test": "Test, VA"},
    }
    plan_path = root / "plan.json.gz"
    with gzip.open(plan_path, "wt", encoding="utf-8") as stream:
        json.dump(plan, stream)
    output = root / "pools"
    arguments = [
        "--plan", str(plan_path), "--tile-graphs", str(graphs), "--output", str(output),
        "--targets", "1", "--surfaces", "road", "--shard-count", "1", "--shard-index", "0",
    ]
    assert precompute_pools.main(arguments) == 0
    with gzip.open(output / "cville-000-p00.jsonl.gz", "rt") as stream:
        stream.readline()
        pooled = {json.loads(line)["cell"] for line in stream}
    return {"cells": cells, "pooled": pooled, "plan": plan_path, "output": output, "arguments": arguments}


def test_each_unit_writes_one_shard_of_its_walkable_cells(tile_run) -> None:
    output = tile_run["output"]
    assert sorted(path.name for path in output.iterdir()) == ["cville-000-p00.jsonl.gz", "sea-000-p00.jsonl.gz"]

    with gzip.open(output / "cville-000-p00.jsonl.gz", "rt") as stream:
        header = json.loads(stream.readline())
        records = [json.loads(line) for line in stream]
    assert header["tile"] == "cville-000" and header["targets"] == [1]
    # A sparse cell may have no 1 mile loop; the cell with no node is never run.
    assert records and {record["cell"] for record in records} <= set(tile_run["cells"])
    with gzip.open(output / "sea-000-p00.jsonl.gz", "rt") as stream:
        assert len(stream.readlines()) == 1  # header only


def test_only_cells_holding_a_tile_node_are_run(tile_run) -> None:
    area = next(item for item in areas() if item.slug == "charlottesville-va")
    walkable = precompute_pools.cells_with_nodes(load_area_graph(area), 200.0)
    assert set(tile_run["cells"]) <= walkable
    assert "200:0:0" not in walkable


def test_rerun_skips_finished_units(tile_run, capsys) -> None:
    assert precompute_pools.main(tile_run["arguments"]) == 0
    assert capsys.readouterr().out.count("already written") == 2


def test_plan_refuses_targets_past_the_tile_size(tile_run, tmp_path: Path) -> None:
    arguments = list(tile_run["arguments"])
    arguments[arguments.index("--output") + 1] = str(tmp_path)
    arguments[arguments.index("--targets") + 1] = "15"
    assert precompute_pools.main(arguments) == 1


def test_merge_takes_every_unit_of_the_plan(tile_run) -> None:
    output = tile_run["output"]
    assert merge_pools.main(["--plan", str(tile_run["plan"]), "--input", str(output)]) == 0

    database = sqlite3.connect(output / "pools.sqlite")
    rows = database.execute("SELECT cell, pool FROM pools").fetchall()
    assert {cell for cell, _ in rows} == tile_run["pooled"]
    assert all(decode_pool(pool)["loops"] for _, pool in rows)
    tiles = dict(database.execute("SELECT tile, graph FROM tiles"))
    assert tiles["sea-000"] is None and len(tiles["cville-000"]) == 16


def test_merge_waits_for_missing_units(tile_run, tmp_path: Path) -> None:
    shutil.copy(tile_run["output"] / "cville-000-p00.jsonl.gz", tmp_path)
    assert merge_pools.main(["--plan", str(tile_run["plan"]), "--input", str(tmp_path)]) == 1
    assert not (tmp_path / "pools.sqlite").exists()
