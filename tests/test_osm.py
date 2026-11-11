from __future__ import annotations

import json
import math
from pathlib import Path

import networkx as nx
import pytest

from runline.coverage import areas, load_area_graph
from runline.geo import destination
from runline.models import (
    Coordinate,
    ElevationPreference,
    RouteCandidate,
    RouteMetrics,
    RoutePreferences,
    SurfacePreference,
)
from runline.osm import (
    _distinct_by_length,
    _major_crossings,
    _nearest_node,
    _compactness,
    _routable_core,
    _separated_return,
    _trim_spurs,
    collapse_graph,
    doubled_back_fraction,
    is_signal,
    measure_route,
)
from runline.planner import plan_routes
from runline.scoring import MIN_COMPACTNESS


def _graph() -> nx.MultiDiGraph:
    graph = nx.MultiDiGraph()
    graph.graph["crs"] = "epsg:4326"
    graph.add_node(1, x=-78.50, y=38.00, elevation=100)
    graph.add_node(2, x=-78.49, y=38.01, elevation=115, highway="traffic_signals")
    graph.add_node(3, x=-78.50, y=38.02, elevation=110)
    graph.add_edge(1, 2, length=1_000, highway="residential", surface="asphalt")
    graph.add_edge(2, 3, length=1_000, highway="path", surface="gravel")
    graph.add_edge(3, 2, length=1_000, highway="path", surface="gravel")
    graph.add_edge(2, 1, length=1_000, highway="residential", surface="asphalt")
    return graph


def test_measure_route_counts_signals_elevation_surface_and_repetition() -> None:
    preferences = RoutePreferences(target_distance_miles=2)
    graph = collapse_graph(_graph(), preferences)

    metrics = measure_route(graph, [1, 2, 3, 2, 1])

    assert metrics.distance_miles == pytest.approx(4_000 / 1609.344)
    assert metrics.elevation_gain_feet == pytest.approx(20 * 3.28084)
    assert metrics.elevation_coverage == 1
    assert metrics.traffic_signal_events == 2
    assert metrics.trail_fraction == pytest.approx(0.5)
    assert metrics.repeated_fraction == pytest.approx(0.5)


@pytest.mark.parametrize(
    "tags",
    [
        {"highway": "traffic_signals"},
        {"highway": "crossing", "crossing": "traffic_signals"},
        {"highway": "crossing", "crossing": "traffic_signals;marked"},
        {"highway": "crossing", "crossing": "pedestrian_signals"},
        {"highway": "crossing", "crossing": "marked", "crossing:signals": "yes"},
    ],
)
def test_signalled_crossings_are_signals(tags) -> None:
    assert is_signal(tags)


@pytest.mark.parametrize(
    "tags",
    [
        {"highway": "crossing"},
        {"highway": "crossing", "crossing": "uncontrolled"},
        {"highway": "crossing", "crossing": "marked", "crossing:signals": "no"},
        {"highway": "stop"},
    ],
)
def test_other_crossings_are_not_signals(tags) -> None:
    assert not is_signal(tags)


def _signal_street(spacing: float) -> nx.MultiDiGraph:
    """A street through 1-2-3-4-5: a crosswalk, a signal and a crosswalk in the middle."""

    graph = nx.MultiDiGraph()
    tags = {2: {"highway": "crossing", "crossing": "traffic_signals"}, 3: {"highway": "traffic_signals"},
            4: {"highway": "crossing", "crossing:signals": "yes"}}
    for node in range(1, 6):
        graph.add_node(node, x=node * 0.001, y=0.0, **tags.get(node, {}))
    lengths = {(1, 2): 200, (2, 3): spacing, (3, 4): spacing, (4, 5): 200}
    for (u, v), length in lengths.items():
        graph.add_edge(u, v, length=length, highway="residential")
        graph.add_edge(v, u, length=length, highway="residential")
    return graph


def test_walking_through_a_signalled_junction_is_one_signal() -> None:
    preferences = RoutePreferences(target_distance_miles=1)
    graph = collapse_graph(_signal_street(15), preferences)

    assert measure_route(graph, [1, 2, 3, 4, 5]).traffic_signal_events == 1
    assert graph.edges[1, 2]["_cost"] == 200 + 450
    assert graph.edges[2, 3]["_cost"] == graph.edges[3, 4]["_cost"] == 15


def test_signals_a_block_apart_each_count() -> None:
    preferences = RoutePreferences(target_distance_miles=1)
    graph = collapse_graph(_signal_street(120), preferences)

    assert measure_route(graph, [1, 2, 3, 4, 5]).traffic_signal_events == 3
    assert graph.edges[2, 3]["_cost"] == 120 + 450


def test_trail_preference_changes_edge_cost() -> None:
    graph = nx.MultiDiGraph()
    graph.add_node(1, x=0, y=0)
    graph.add_node(2, x=1, y=1)
    graph.add_edge(1, 2, length=100, highway="residential", surface="asphalt")
    graph.add_edge(1, 2, length=110, highway="path", surface="gravel")

    road = collapse_graph(
        graph,
        RoutePreferences(
            target_distance_miles=3,
            surface=SurfacePreference.ROAD,
            elevation=ElevationPreference.BALANCED,
        ),
    )
    trail = collapse_graph(
        graph,
        RoutePreferences(
            target_distance_miles=3,
            surface=SurfacePreference.TRAIL,
            elevation=ElevationPreference.BALANCED,
        ),
    )

    assert road.edges[1, 2]["highway"] == "residential"
    assert trail.edges[1, 2]["highway"] == "path"


def test_minor_road_entry_at_major_junction_costs_more() -> None:
    graph = nx.MultiDiGraph()
    for node in (1, 2, 3, 4):
        graph.add_node(node, x=0, y=0)
    graph.add_edge(1, 2, length=100, highway="primary")
    graph.add_edge(2, 3, length=100, highway="primary")
    graph.add_edge(4, 2, length=100, highway="residential")
    graph.add_edge(2, 4, length=100, highway="residential")

    collapsed = collapse_graph(graph, RoutePreferences(3))

    assert collapsed.edges[4, 2]["_cost"] == pytest.approx(400)
    assert collapsed.edges[2, 4]["_cost"] == pytest.approx(100)


def test_divided_major_road_counts_as_one_crossing() -> None:
    graph = nx.DiGraph()
    for node, longitude in enumerate((0, .0001, .0002, .0003), start=1):
        graph.add_node(node, x=longitude, y=0)
    for u, v in ((1, 2), (2, 3), (3, 4)):
        graph.add_edge(u, v, length=11, highway="residential")
    for junction in (2, 3):
        for offset in (-.0001, .0001):
            spur = junction * 10 + (1 if offset > 0 else 0)
            graph.add_node(spur, x=graph.nodes[junction]["x"], y=offset)
            graph.add_edge(junction, spur, length=11, highway="primary", name="Main Street")

    assert measure_route(graph, [1, 2, 3, 4]).major_crossing_events == 1

    graph.edges[3, 30]["name"] = "Other Street"
    graph.edges[3, 31]["name"] = "Other Street"
    assert measure_route(graph, [1, 2, 3, 4]).major_crossing_events == 2


def test_crossing_count_against_geometry_labels() -> None:
    # Route slices around every major-road node on generated routes, labelled
    # by scripts/audit_crossings.py from corridor geometry, not by this rule.
    fixture = Path(__file__).parent / "fixtures/crossing_audit.json"
    slices = json.loads(fixture.read_text())
    graph = collapse_graph(load_area_graph(areas()[0]), RoutePreferences(3))
    # Match positions, not just counts: an extra crossing cannot cancel a miss.
    from scripts.audit_crossings import match

    counted = labelled = agreed = 0
    for route_slice in slices:
        proxy = _major_crossings(graph, route_slice["nodes"])
        labels = [tuple(event) for event in route_slice["events"]]
        tp, fp, fn = match(proxy, labels)
        counted += tp + fp
        labelled += tp + fn
        agreed += tp

    assert labelled >= 100
    assert agreed / counted >= 0.9
    assert agreed / labelled >= 0.9


def test_same_side_turn_at_major_road_is_not_a_crossing() -> None:
    graph = nx.DiGraph()
    # Main Street runs north-south through node 2; the route stays west of it.
    for node, (x, y) in {1: (-.0002, .0002), 2: (0, 0), 3: (-.0002, -.0002),
                         4: (0, .0003), 5: (0, -.0003), 6: (.0002, -.0002)}.items():
        graph.add_node(node, x=x, y=y)
    for u, v in ((1, 2), (2, 3), (2, 6)):
        graph.add_edge(u, v, length=30, highway="footway")
    for u in (4, 5):
        graph.add_edge(2, u, length=33, highway="primary", name="Main Street")
        graph.add_edge(u, 2, length=33, highway="primary", name="Main Street")

    assert measure_route(graph, [1, 2, 3]).major_crossing_events == 0
    assert measure_route(graph, [1, 2, 6]).major_crossing_events == 1


def test_leaving_a_major_road_on_the_other_side_is_a_crossing() -> None:
    graph = nx.DiGraph()
    # The route joins Main Street from the west, runs north, then leaves.
    for node, (x, y) in {1: (-.0003, 0), 2: (0, 0), 3: (0, .0003),
                         4: (.0003, .0003), 5: (-.0003, .0003)}.items():
        graph.add_node(node, x=x, y=y)
    graph.add_edge(1, 2, length=26, highway="residential")
    graph.add_edge(2, 3, length=33, highway="primary", name="Main Street")
    graph.add_edge(3, 4, length=26, highway="residential")
    graph.add_edge(3, 5, length=26, highway="residential")

    assert measure_route(graph, [1, 2, 3, 4]).major_crossing_events == 1
    assert measure_route(graph, [1, 2, 3, 5]).major_crossing_events == 0


def test_walking_along_a_major_road_past_a_major_side_road_crosses_it() -> None:
    graph = nx.DiGraph()
    # The route walks north along the west side of Main Street; Side Road
    # branches west at node 3 and east at node 4.
    for node, (x, y) in {1: (-.0003, 0), 2: (0, 0), 3: (0, .0003), 4: (0, .0006),
                         5: (0, .0009), 6: (-.0003, .0009), 7: (-.0003, .0003),
                         8: (.0003, .0006)}.items():
        graph.add_node(node, x=x, y=y)
    graph.add_edge(1, 2, length=26, highway="residential")
    for u, v in ((2, 3), (3, 4), (4, 5)):
        graph.add_edge(u, v, length=33, highway="primary", name="Main Street")
    graph.add_edge(5, 6, length=26, highway="residential")
    graph.add_edge(3, 7, length=26, highway="primary", name="Side Road")
    graph.add_edge(4, 8, length=26, highway="primary", name="East Road")

    assert measure_route(graph, [1, 2, 3, 4, 5, 6]).major_crossing_events == 1


def test_weighted_dedup_keeps_better_route_with_same_long_edges() -> None:
    graph = nx.DiGraph()
    for u, v, length in ((1, 2, 1000), (2, 3, 1000), (3, 4, 1), (4, 5, 1)):
        graph.add_edge(u, v, length=length)
    metrics = RouteMetrics(2, 0, 0, 0, 0, 0)
    better = RouteCandidate("better", (1, 2, 3), (), metrics)
    worse = RouteCandidate("worse", (1, 2, 3, 4, 5), (), metrics)

    assert _distinct_by_length(graph, [better, worse], 2) == [better]


def test_collapse_preserves_osm_graph_metadata() -> None:
    collapsed = collapse_graph(_graph(), RoutePreferences(target_distance_miles=3))

    assert collapsed.graph["crs"] == "epsg:4326"


def test_nearest_node_does_not_require_optional_spatial_index() -> None:
    graph = _graph()

    assert _nearest_node(graph, Coordinate(38.0101, -78.4899)) == 2


def test_routable_core_drops_disconnected_islands() -> None:
    """A start point on a stranded fragment made every waypoint unreachable."""

    graph = nx.DiGraph()
    for node, (latitude, longitude) in enumerate(
        [(38.00, -78.00), (38.01, -78.00), (38.00, -78.01)]
    ):
        graph.add_node(node, y=latitude, x=longitude)
    graph.add_edges_from([(0, 1), (1, 2), (2, 0)])

    # An island sitting nearest to the query point but reachable from nothing.
    graph.add_node(99, y=38.005, x=-78.005)
    graph.add_node(98, y=38.0051, x=-78.0051)
    graph.add_edge(99, 98)

    core = _routable_core(graph)

    assert set(core.nodes) == {0, 1, 2}
    assert _nearest_node(core, Coordinate(38.005, -78.005)) in {0, 1, 2}


def test_routable_core_leaves_a_fully_connected_graph_alone() -> None:
    graph = nx.DiGraph()
    for node, (latitude, longitude) in enumerate(
        [(38.00, -78.00), (38.01, -78.00), (38.00, -78.01)]
    ):
        graph.add_node(node, y=latitude, x=longitude)
    graph.add_edges_from([(0, 1), (1, 2), (2, 0)])

    assert _routable_core(graph) is graph


def test_rotunda_12_miles_is_served_with_limited_coverage(tmp_path, monkeypatch) -> None:
    # The Rotunda is 2.4 km off the shipped area's centre, so a 12 mile crop
    # overhangs the map; it used to fail with downloads disabled.
    monkeypatch.setenv("RUNLINE_ALLOW_OSM_DOWNLOAD", "0")
    preferences = RoutePreferences(12)

    result = plan_routes(
        Coordinate(38.035556, -78.503333), preferences,
        cache_directory=tmp_path, elevation_source="none",
    )

    assert len(result.candidates) == 3
    assert all(abs(route.metrics.distance_miles - 12) <= 0.25 for route in result.candidates)
    assert any("coverage is limited" in warning for warning in result.warnings)


def test_trim_spurs_removes_dead_end_detours() -> None:
    # 1 -> 2 -> 3 -> 9 (dead end) -> 3 -> 4 -> 1, with a two-edge spur at 4.
    route = [1, 2, 3, 9, 3, 4, 7, 8, 7, 4, 1]
    assert _trim_spurs(route) == [1, 2, 3, 4, 1]


def test_trim_spurs_keeps_a_loop_around_a_block() -> None:
    route = [1, 2, 3, 4, 2, 5, 1]
    assert _trim_spurs(route) == route


def test_compactness_tells_a_square_from_a_sliver() -> None:
    graph = nx.DiGraph()
    corners = {1: (0, 0), 2: (0.01, 0), 3: (0.01, 0.01), 4: (0, 0.01), 5: (0.01, 0.0005), 6: (0, 0.0005)}
    for node, (x, y) in corners.items():
        graph.add_node(node, x=x, y=y)

    square = _compactness(graph, [1, 2, 3, 4, 1])
    sliver = _compactness(graph, [1, 2, 5, 6, 1])

    assert 0.7 < square < 0.85
    assert sliver < 0.2


def _compactness_graph(points: dict[int, tuple[float, float]]) -> nx.DiGraph:
    graph = nx.DiGraph()
    for node, (x, y) in points.items():
        graph.add_node(node, x=x * 0.01, y=y * 0.01)
    return graph


def _shoelace_compactness(graph: nx.DiGraph, node_ids: list[int]) -> float:
    """The signed-area formula, which is right for a loop that never crosses itself."""

    points = [(graph.nodes[n]["x"] * 111_320.0, graph.nodes[n]["y"] * 110_540.0) for n in node_ids]
    pairs = list(zip(points, points[1:] + points[:1]))
    area = sum(x1 * y2 - x2 * y1 for (x1, y1), (x2, y2) in pairs)
    perimeter = sum(((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5 for (x1, y1), (x2, y2) in pairs)
    return min(1.0, 4 * math.pi * abs(area) / 2 / perimeter**2)


def test_compactness_of_a_simple_loop_is_unchanged() -> None:
    graph = _compactness_graph({1: (0, 0), 2: (3, 0), 3: (4, 2), 4: (1, 3), 5: (-1, 1)})
    route = [1, 2, 3, 4, 5, 1]

    assert _compactness(graph, route) == pytest.approx(_shoelace_compactness(graph, route), rel=1e-12)


def test_figure_eight_lobes_do_not_cancel() -> None:
    # Two diamonds meeting at node 1, run in opposite directions, as a runner
    # would draw an 8: the signed areas of the lobes cancel to zero.
    graph = _compactness_graph(
        {1: (0, 0), 2: (1, 1), 3: (2, 0), 4: (1, -1), 5: (-1, 1), 6: (-2, 0), 7: (-1, -1)}
    )
    eight = [1, 2, 3, 4, 1, 5, 6, 7, 1]
    lobe = [1, 2, 3, 4, 1]

    assert _shoelace_compactness(graph, eight) == pytest.approx(0, abs=1e-9)
    # Twice the area and twice the perimeter: half one lobe's ratio.
    assert _compactness(graph, eight) == pytest.approx(_compactness(graph, lobe) / 2)
    assert _compactness(graph, eight) > MIN_COMPACTNESS


def test_bowtie_crossing_between_nodes_counts_both_lobes() -> None:
    graph = _compactness_graph({1: (0, 0), 2: (2, 2), 3: (2, 0), 4: (0, 2)})
    bowtie = [1, 2, 3, 4, 1]
    triangle = _compactness_graph({1: (0, 0), 2: (1, 1), 3: (0, 2), 4: (0, 1)})

    assert _shoelace_compactness(graph, bowtie) == pytest.approx(0, abs=1e-9)
    # Two triangles of half the bowtie's perimeter each.
    assert _compactness(graph, bowtie) == pytest.approx(_compactness(triangle, [1, 2, 3, 4, 1]) / 2, rel=1e-3)


def test_a_spur_off_a_loop_adds_no_area() -> None:
    graph = _compactness_graph({1: (0, 0), 2: (2, 0), 3: (2, 2), 4: (0, 2), 5: (3, 3)})
    with_spur = [1, 2, 3, 5, 3, 4, 1]

    assert _compactness(graph, with_spur) == pytest.approx(_shoelace_compactness(graph, with_spur))


def test_separated_return_stays_off_the_way_out() -> None:
    # Out 1 -> 2 -> 3. Home is 3 -> 2 -> 1 (cost 2) or 3 -> 4 -> 1 (cost 3).
    graph = nx.DiGraph()
    for u, v, cost in ((1, 2, 1), (2, 3, 1), (3, 2, 1), (2, 1, 1), (3, 4, 1.5), (4, 1, 1.5)):
        graph.add_edge(u, v, _cost=cost)

    assert nx.shortest_path(graph, 3, 1, weight="_cost") == [3, 2, 1]
    assert _separated_return(graph, [1, 2, 3], 3, 1) == [3, 4, 1]



def test_bounded_search_returns_the_same_paths_as_a_full_one() -> None:
    from runline.osm import _cost_matrix

    graph = nx.DiGraph()
    for node in range(30):
        for other, cost in (((node + 1) % 30, 10.0 + node % 7), ((node + 7) % 30, 55.0)):
            graph.add_edge(node, other, _cost=cost)
            graph.add_edge(other, node, _cost=cost * 1.3)
    graph.add_node(99)  # unreachable from everything
    costs = _cost_matrix(graph)
    full = costs.tree(0)

    for limit in (0.0, 15.0, 60.0, 1e9):
        bounded = costs.tree(0, limit=limit)
        for target in range(30):
            assert bounded[target] == full[target]
        with pytest.raises(KeyError):
            costs.tree(0, limit=limit)[99]


def test_major_edge_memo_is_rebuilt_when_the_graph_changes() -> None:
    from runline.osm import _major_edge_index, _major_edges_at

    graph = nx.DiGraph()
    graph.add_edge(1, 2, highway="primary")
    graph.add_edge(2, 3, highway="residential")
    index = _major_edge_index(graph)
    assert set(_major_edges_at(graph, index, 2)) == {1}
    assert _major_edges_at(graph, index, 3) == {}
    assert set(_major_edge_index(graph)) == {2, 3}

    graph.add_edge(3, 4, highway="primary")
    assert _major_edge_index(graph) == {}


def _walk(*legs: tuple[float, float]) -> list[Coordinate]:
    """Coordinates from a start near Charlottesville, one point per (bearing, metres) leg."""

    points = [Coordinate(38.03, -78.48)]
    for bearing, meters in legs:
        points.append(destination(points[-1], bearing, meters))
    return points


def test_out_on_a_street_and_back_on_its_sidewalk_counts_as_doubling_back() -> None:
    out_and_back = _walk((90, 800), (0, 12), (270, 800))
    assert doubled_back_fraction(out_and_back) == pytest.approx(0.5, abs=0.03)


def test_a_round_loop_and_a_lap_do_not_count_as_doubling_back() -> None:
    square = _walk((90, 500), (0, 500), (270, 500), (180, 500))
    assert doubled_back_fraction(square) == 0
    # Two laps of the same block run the same way.
    assert doubled_back_fraction(square + square[1:]) == 0


def test_a_spur_counts_only_its_own_length() -> None:
    # A 2 km square with a 100 m out-and-back on its far side: the way back is
    # 100 of 2,212 m.
    loop = _walk((90, 500), (0, 250), (90, 100), (0, 12), (270, 100), (0, 238), (270, 500), (180, 500))
    assert doubled_back_fraction(loop) == pytest.approx(100 / 2212, abs=0.01)


def test_parallel_streets_further_apart_do_not_count() -> None:
    assert doubled_back_fraction(_walk((90, 800), (0, 60), (270, 800))) == 0


def test_measured_repeats_include_doubling_back_on_the_drawn_route(tmp_path, monkeypatch) -> None:
    # measure_route works from cached per-edge lines; it must agree with the
    # drawn coordinates the runner sees.
    monkeypatch.setenv("RUNLINE_ALLOW_OSM_DOWNLOAD", "0")
    result = plan_routes(
        Coordinate(38.035556, -78.503333), RoutePreferences(5),
        cache_directory=tmp_path, elevation_source="none",
    )

    for route in result.candidates:
        assert route.metrics.repeated_fraction >= doubled_back_fraction(route.coordinates) - 0.005
