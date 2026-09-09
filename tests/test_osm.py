from __future__ import annotations

import networkx as nx
import pytest

from runline.models import Coordinate, ElevationPreference, RoutePreferences, SurfacePreference
from runline.osm import _nearest_node, _routable_core, collapse_graph, measure_route


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
