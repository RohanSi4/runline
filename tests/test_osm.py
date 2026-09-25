from __future__ import annotations

import json
from pathlib import Path

import networkx as nx
import pytest

from runline.coverage import areas, load_area_graph
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
    _nearest_node,
    _path,
    _routable_core,
    collapse_graph,
    measure_route,
)


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


def test_crossing_proxy_against_geometry_labels() -> None:
    fixture = Path(__file__).parent / "fixtures/crossing_audit.json"
    samples = json.loads(fixture.read_text())
    graph = collapse_graph(load_area_graph(areas()[0]), RoutePreferences(3))
    true_positive = false_positive = false_negative = 0
    for sample in samples:
        proxy = measure_route(graph, sample["nodes"]).major_crossing_events == 1
        label = sample["geometry_crossing"]
        true_positive += proxy and label
        false_positive += proxy and not label
        false_negative += not proxy and label

    assert len(samples) == 100
    assert true_positive / (true_positive + false_positive) >= 0.9
    assert true_positive / (true_positive + false_negative) >= 0.9


def test_astar_scales_heuristic_for_inaccurate_edge_lengths() -> None:
    graph = nx.DiGraph()
    for node, longitude in ((1, 0), (2, 1), (3, 2)):
        graph.add_node(node, x=longitude, y=0)
    graph.add_edge(1, 2, _cost=1)
    graph.add_edge(2, 3, _cost=1)
    graph.add_edge(1, 3, _cost=100)

    assert _path(graph, 1, 3) == [1, 2, 3]


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
