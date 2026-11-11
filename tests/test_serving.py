"""Requests answered from a real pool, built here for one Charlottesville cell."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from dataclasses import asdict
from pathlib import Path

import pytest

from runline import serving
from runline.coverage import areas, graphs_root, load_area_graph
from runline.geo import destination, distance_meters
from runline.grid import cell_centre, cell_id, neighbouring_cells
from runline.models import Coordinate, RoutePreferences, SurfacePreference
from runline.osm import _nearest_node, doubled_back_fraction, signal_events
from runline.planner import plan_routes
from runline.pools import FORMAT_VERSION, encode_pool
from scripts.precompute_pools import area_cells, cell_origin, cell_pool, prepare_area

TARGETS = (4, 5, 6)
MILES = 5


def _write_pool(directory: Path, area, pool: dict, surface: str, cell: str, *, format_version=FORMAT_VERSION, graph=None) -> None:
    fingerprint = graph or hashlib.sha256((graphs_root() / area.filename).read_bytes()).hexdigest()[:16]
    database = sqlite3.connect(directory / f"{area.slug}.sqlite")
    database.executescript(
        """
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE pools (cell TEXT, surface TEXT, loops INTEGER, pool BLOB, PRIMARY KEY (cell, surface));
        """
    )
    meta = {"format": format_version, "graph": fingerprint, "grid_meters": 200.0}
    database.executemany("INSERT INTO meta VALUES (?, ?)", [(k, json.dumps(v)) for k, v in meta.items()])
    database.execute("INSERT INTO pools VALUES (?, ?, ?, ?)", (cell, surface, len(pool["loops"]), encode_pool(pool)))
    database.commit()
    database.close()


@pytest.fixture(scope="module")
def built():
    area = next(item for item in areas() if item.slug == "charlottesville-va")
    graph = load_area_graph(area)
    cell = cell_id(area.center.latitude, area.center.longitude, 200.0)
    core = prepare_area(graph, SurfacePreference.MIXED)
    pool = cell_pool(core, cell, TARGETS, SurfacePreference.MIXED)
    return area, graph, cell, pool


@pytest.fixture
def pools_dir(built, tmp_path: Path, monkeypatch) -> Path:
    area, _, cell, pool = built
    _write_pool(tmp_path, area, pool, "mixed", cell)
    monkeypatch.setenv("RUNLINE_POOLS_DIR", str(tmp_path))
    serving._checked_pool_file.cache_clear()
    return tmp_path


def _preferences(**changes) -> RoutePreferences:
    return RoutePreferences(MILES, surface=SurfacePreference.MIXED, **changes)


def test_neighbour_pools_stay_on_the_loaded_tile_graph(tmp_path: Path) -> None:
    pool = {"origin_node": 1, "origin": [0.0, 0.0], "loops": []}
    path = tmp_path / "metro.sqlite"
    with sqlite3.connect(path) as database:
        database.execute("CREATE TABLE pools (cell TEXT, surface TEXT, tile TEXT, pool BLOB)")
        database.executemany(
            "INSERT INTO pools VALUES (?, ?, ?, ?)",
            [
                ("own", "mixed", "tile-a", encode_pool(pool)),
                # This row must never be decoded on tile-a, even if the
                # caller includes this neighbour in its lookup.
                ("neighbour", "mixed", "tile-b", b"not this graph's pool"),
            ],
        )
    source = serving.PoolFile(path, 200.0, tile="tile-a")

    assert serving.load_pools(source, ["neighbour", "own"], "mixed") == [pool]
    assert serving.load_pool(source, "neighbour", "mixed") is None


def _origin(built) -> Coordinate:
    return Coordinate(*built[3]["origin"])


def test_a_hit_returns_full_loops_in_the_distance_band(built, pools_dir) -> None:
    routes = serving.serve_from_pool(_origin(built), _preferences())

    assert routes is not None and len(routes) == 3
    for route in routes:
        assert abs(route.metrics.distance_miles - MILES) <= 0.25
        assert route.node_ids[0] == route.node_ids[-1] == built[3]["origin_node"]
        assert len(route.coordinates) > len(route.node_ids) / 2
        assert route.start.kind == "origin"
    assert [route.route_id for route in routes] == ["route-001", "route-002", "route-003"]
    assert len({route.node_ids for route in routes}) == 3


def test_ranking_matches_the_live_rules(built, pools_dir) -> None:
    routes = serving.serve_from_pool(_origin(built), _preferences())

    assert all(route.score_components for route in routes)
    round_first = [route.metrics.compactness < 0.2 for route in routes]
    assert round_first == sorted(round_first)


@pytest.mark.parametrize("available", [1, 2])
def test_two_distinct_in_band_routes_can_answer_but_one_falls_back(built, pools_dir, available) -> None:
    original = serving.serve_from_pool(_origin(built), _preferences())
    area, _, _, pool = built
    replacement = {
        **pool,
        "loops": [{"nodes": list(r.node_ids), "metrics": asdict(r.metrics)} for r in original[:available]],
    }
    with sqlite3.connect(pools_dir / f"{area.slug}.sqlite") as database:
        database.execute("UPDATE pools SET pool = ?, loops = ?", (encode_pool(replacement), available))

    if available == 1:
        assert serving.serve_from_pool(_origin(built), _preferences()) is None
    else:
        result = plan_routes(_origin(built), _preferences(), cache_directory=pools_dir, elevation_source="none")
        assert result.source == "pool" and len(result.candidates) == 2
        assert "2 distinct routes" in result.warnings[0]
        assert all(abs(r.metrics.distance_miles - MILES) <= 0.25 for r in result.candidates)
        assert all(r.metrics.repeated_fraction <= serving.MAX_DOUBLED_BACK for r in result.candidates)


def _loop_nodes(pool: dict) -> set[int]:
    return {node for loop in pool["loops"] for node in loop["nodes"]}


def _cell_nodes_by_reach(built) -> tuple[list[int], list[int]]:
    """Other walkable nodes in the cell: (some pool loop within reach, none)."""

    _, graph, cell, pool = built
    loops = _loop_nodes(pool)
    others = [
        node for node, data in graph.nodes(data=True)
        if cell_id(float(data["y"]), float(data["x"]), 200.0) == cell and node != pool["origin_node"]
    ]
    reached = []
    for node in others:
        reach = serving._reach(graph, node)
        if loops & reach.out_meters.keys() & reach.back_meters.keys():
            reached.append(node)
    return reached, [node for node in others if node not in reached]


def _at(graph, node) -> Coordinate:
    return Coordinate(float(graph.nodes[node]["y"]), float(graph.nodes[node]["x"]))


def _is_rotation_of_a_loop(route: tuple[int, ...], pool: dict) -> bool:
    """The route runs some pool loop from wherever it joins it.

    Nearly all of it: restarting a loop can put the old origin's out-and-back
    spur mid-route, where it is trimmed as live routes trim theirs.
    """

    steps = set(zip(route, route[1:]))
    for loop in pool["loops"]:
        edges = list(zip(loop["nodes"], loop["nodes"][1:]))
        if sum(edge in steps for edge in edges) >= 0.9 * len(edges):
            return True
    return False


def test_a_start_elsewhere_in_the_cell_joins_a_loop_where_it_passes_closest(built, pools_dir) -> None:
    _, graph, _, pool = built
    reached, _ = _cell_nodes_by_reach(built)
    node = reached[0]
    origin = _at(graph, node)
    assert _nearest_node(graph, origin) == node

    routes = serving.serve_from_pool(origin, _preferences(result_count=1))

    assert routes
    for route in routes:
        assert route.node_ids[0] == route.node_ids[-1] == node
        assert _is_rotation_of_a_loop(route.node_ids, pool)
        assert distance_meters(route.coordinates[0], origin) < 1
        assert distance_meters(route.coordinates[-1], origin) < 1


def test_a_start_with_no_loop_within_reach_falls_back(built, pools_dir, monkeypatch) -> None:
    _, graph, _, pool = built
    reached, _ = _cell_nodes_by_reach(built)
    off_the_loops = next(node for node in reached if node not in _loop_nodes(pool))
    monkeypatch.setattr(serving, "MAX_CONNECTOR_METERS", 0.0)
    assert serving.serve_from_pool(_at(graph, off_the_loops), _preferences(result_count=1)) is None


def test_anchoring_restarts_the_loop_at_its_nearest_node_and_adds_the_way_there() -> None:
    reach = serving._Reach(
        out_meters={10: 0.0, 11: 40.0, 3: 90.0, 2: 150.0},
        out_paths={10: [10], 11: [10, 11], 3: [10, 11, 3], 2: [10, 11, 3, 2]},
        back_meters={10: 0.0, 12: 30.0, 3: 80.0, 2: 160.0},
        back_paths={10: [10], 12: [10, 12], 3: [10, 12, 3], 2: [10, 12, 3, 2]},
    )

    nodes, meters = serving._anchored([1, 2, 3, 4, 1], reach)

    assert nodes == (10, 11, 3, 4, 1, 2, 3, 12, 10)
    assert meters == 170.0
    assert serving._anchored([5, 6, 7, 5], reach) is None


def test_a_connector_is_measured_like_the_rest_of_the_route(built, pools_dir) -> None:
    """Length, signals and climbing on the way to the loop count, from the drawn route."""

    _, graph, _, _ = built
    reached, _ = _cell_nodes_by_reach(built)
    route = next(
        routes[0]
        for routes in (serving.serve_from_pool(_at(graph, node), _preferences(result_count=1)) for node in reached)
        if routes
    )
    nodes = route.node_ids

    def cheapest(u, v):
        return min(
            graph[u][v].values(),
            key=lambda data: serving._edge_preference_cost(data, graph.nodes[v], _preferences(), graph.nodes[u]),
        )

    meters = sum(float(cheapest(u, v)["length"]) for u, v in zip(nodes, nodes[1:]))
    small = serving._route_graph(graph, [nodes], _preferences())
    signals = signal_events(small, list(nodes))
    assert route.metrics.distance_miles == pytest.approx(meters / 1609.344)
    assert route.metrics.traffic_signal_events == signals
    assert abs(route.metrics.distance_miles - MILES) <= 0.25


def test_a_cell_with_no_pool_of_its_own_is_answered_from_a_neighbour(built, pools_dir) -> None:
    _, graph, cell, pool = built
    neighbours = set(neighbouring_cells(cell)[1:])
    loops = _loop_nodes(pool)
    for node, data in graph.nodes(data=True):
        if cell_id(float(data["y"]), float(data["x"]), 200.0) not in neighbours:
            continue
        reach = serving._reach(graph, node)
        if not loops & reach.out_meters.keys() & reach.back_meters.keys():
            continue
        routes = serving.serve_from_pool(_at(graph, node), _preferences(result_count=1))
        if routes:
            assert routes[0].node_ids[0] == routes[0].node_ids[-1] == node
            assert _is_rotation_of_a_loop(routes[0].node_ids, pool)
            return
    pytest.fail("no start in a neighbouring cell was answered from this cell's pool")


def test_neighbouring_cells_are_the_eight_around_the_cell() -> None:
    cell = cell_id(38.03, -78.48, 200.0)
    cells = neighbouring_cells(cell)

    assert cells[0] == cell and len(set(cells)) == 9
    centre = cell_centre(cell)
    assert all(distance_meters(centre, cell_centre(other)) < 320 for other in cells)
    # Columns are staggered from row to row, so check what the nine cover:
    # every point up to 150 m from the centre each way.
    for bearing in range(0, 360, 15):
        for meters in (50, 100, 150):
            point = destination(centre, bearing, meters)
            assert cell_id(point.latitude, point.longitude, 200.0) in cells


def test_pools_keep_no_loop_that_serving_would_drop(built) -> None:
    assert all(loop["metrics"]["repeated_fraction"] <= serving.MAX_DOUBLED_BACK for loop in built[3]["loops"])


def test_a_cell_whose_nodes_are_all_off_the_routable_core_gets_no_pool(built) -> None:
    area, graph, _, _ = built
    core = prepare_area(graph, SurfacePreference.MIXED)
    stranded = next(cell for cell in area_cells(area, graph, 200.0) if cell_origin(core, cell) is None)
    assert cell_pool(core, stranded, TARGETS, SurfacePreference.MIXED) is None
    origin = cell_origin(core, built[2])
    data = core.nodes[origin]
    assert cell_id(float(data["y"]), float(data["x"]), 200.0) == built[2]


@pytest.mark.parametrize(
    "changes",
    [{"drive_radius_miles": 3}, {"result_count": 500}],
)
def test_requests_a_pool_cannot_answer_fall_back(built, pools_dir, changes) -> None:
    assert serving.serve_from_pool(_origin(built), _preferences(**changes)) is None


def test_distances_outside_the_pool_fall_back(built, pools_dir) -> None:
    far = RoutePreferences(11, surface=SurfacePreference.MIXED)
    assert serving.serve_from_pool(_origin(built), far) is None


def test_a_cell_with_no_pool_falls_back(built, pools_dir) -> None:
    area = built[0]
    elsewhere = destination(area.center, 90, 1500)
    assert cell_id(elsewhere.latitude, elsewhere.longitude, 200.0) != built[2]
    assert serving.serve_from_pool(elsewhere, _preferences()) is None


def test_another_surface_falls_back(built, pools_dir) -> None:
    trail = RoutePreferences(MILES, surface=SurfacePreference.TRAIL)
    assert serving.serve_from_pool(_origin(built), trail) is None


@pytest.mark.parametrize("bad", [{"format_version": 1}, {"graph": "0" * 16}])
def test_stale_pool_files_are_ignored(built, tmp_path: Path, monkeypatch, bad) -> None:
    area, _, cell, pool = built
    _write_pool(tmp_path, area, pool, "mixed", cell, **bad)
    monkeypatch.setenv("RUNLINE_POOLS_DIR", str(tmp_path))
    serving._checked_pool_file.cache_clear()
    assert serving.serve_from_pool(_origin(built), _preferences()) is None


def test_no_pool_directory_falls_back(built, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("RUNLINE_POOLS_DIR", str(tmp_path / "missing"))
    assert serving.serve_from_pool(_origin(built), _preferences()) is None


def test_the_planner_reports_where_routes_came_from(built, pools_dir, tmp_path: Path, monkeypatch) -> None:
    origin = _origin(built)
    assert plan_routes(origin, _preferences(), cache_directory=tmp_path).source == "pool"

    monkeypatch.setenv("RUNLINE_POOLS_DIR", str(tmp_path / "missing"))
    result = plan_routes(origin, _preferences(), cache_directory=tmp_path, elevation_source="none")
    assert result.source == "live" and result.candidates


def test_a_corrupt_pool_plans_live_instead_of_failing(built, pools_dir, tmp_path: Path, caplog) -> None:
    area, _, cell, _ = built
    database = sqlite3.connect(pools_dir / f"{area.slug}.sqlite")
    database.execute("UPDATE pools SET pool = ? WHERE cell = ?", (b"not lzma at all", cell))
    database.commit()
    database.close()

    with caplog.at_level("ERROR", logger="runline.planner"):
        result = plan_routes(_origin(built), _preferences(), cache_directory=tmp_path, elevation_source="none")

    assert result.source == "live" and result.candidates
    assert "planning live" in caplog.text


def test_the_api_serves_a_pool_route_with_gpx(built, pools_dir) -> None:
    from runline.api import RouteRequest, routes

    origin = _origin(built)
    payload = asyncio.run(
        routes(RouteRequest(latitude=origin.latitude, longitude=origin.longitude, distance_miles=MILES, surface="mixed"))
    )

    assert payload["source"] == "pool"
    assert len(payload["routes"]) == 3
    assert "<gpx" in payload["routes"][0]["gpx"]
    assert len(payload["routes"][0]["feature"]["geometry"]["coordinates"]) > 10


def test_served_routes_count_doubling_back_as_repeated(built, pools_dir) -> None:
    for route in serving.serve_from_pool(_origin(built), _preferences()):
        doubled = doubled_back_fraction(route.coordinates)
        assert doubled <= serving.MAX_DOUBLED_BACK
        assert route.metrics.repeated_fraction >= doubled


def test_a_pool_whose_loops_double_back_falls_back(built, pools_dir, monkeypatch) -> None:
    monkeypatch.setattr(serving, "MAX_DOUBLED_BACK", -1.0)
    assert serving.serve_from_pool(_origin(built), _preferences()) is None
