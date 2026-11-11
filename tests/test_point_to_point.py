from __future__ import annotations

import copy
from pathlib import Path
from xml.etree import ElementTree as ET

import networkx as nx
import pytest
from shapely.geometry import LineString

import runline.point_to_point as direct
from runline.coverage import Area, CoverageError
from runline.gpx import candidate_to_gpx
from runline.models import Coordinate, RoutePreferences, SurfacePreference


@pytest.fixture
def local_map(monkeypatch):
    """Synthetic equatorial graph: a directed path with no route home."""
    graph = nx.MultiDiGraph()
    graph.graph.update(crs="epsg:4326", elevation_baked=True)
    for node, longitude, elevation in ((1, 0.0, 10), (2, .001, 20), (3, .002, 15)):
        graph.add_node(node, x=longitude, y=0.0, elevation=elevation)
    graph.add_node(4, x=.001, y=.001, elevation=12)
    graph.nodes[2]["highway"] = "traffic_signals"
    graph.add_edge(1, 2, length=100, highway="residential",
                   geometry=LineString([(0, 0), (.0004, .0002), (.001, 0)]))
    graph.add_edge(2, 3, length=100, highway="path", surface="gravel")
    area = Area("synthetic", "Synthetic test region", Coordinate(0, 0), 10_000, "synthetic.pkl.gz")
    monkeypatch.setattr(direct, "areas", lambda: (area,))
    monkeypatch.setattr(direct, "load_area_graph", lambda selected: graph)
    return graph, area


def plan(tmp_path: Path, origin=Coordinate(0, 0), finish=Coordinate(0, .002), **preferences):
    return direct.plan_point_to_point(
        origin, finish, RoutePreferences(3, **preferences), cache_directory=tmp_path,
    )


def test_direct_path_retains_real_geometry_metrics_and_gpx(local_map, tmp_path):
    result = plan(tmp_path)
    assert result.source == "live"
    assert result.discovered_starts == result.fetched_elevation_cells == 0
    assert len(result.candidates) == 1
    candidate = result.candidates[0]
    assert candidate.node_ids == (1, 2, 3)
    assert candidate.coordinates == (
        Coordinate(0, 0), Coordinate(.0002, .0004), Coordinate(0, .001), Coordinate(0, .002),
    )
    assert candidate.coordinates[0] != candidate.coordinates[-1]
    assert candidate.start.coordinate == candidate.coordinates[0]
    assert candidate.metrics.distance_miles == pytest.approx(200 / 1609.344)
    assert candidate.metrics.traffic_signal_events == 1
    assert candidate.metrics.trail_fraction == pytest.approx(.5)
    assert candidate.metrics.elevation_gain_feet == pytest.approx(10 * 3.28084)
    assert candidate.metrics.elevation_coverage == 1
    assert candidate.metrics.compactness == 0
    assert candidate.metrics.repeated_fraction == 0
    assert candidate.score_components == {"path_cost": candidate.score}
    track = ET.fromstring(candidate_to_gpx(candidate))
    points = track.findall(".//{*}trkpt")
    assert [(float(point.attrib["lat"]), float(point.attrib["lon"])) for point in points] == [
        (point.latitude, point.longitude) for point in candidate.coordinates
    ]


def test_valid_one_way_path_is_not_dropped_by_loop_connectivity_rules(local_map, tmp_path):
    graph, _ = local_map
    assert not nx.is_strongly_connected(graph)
    assert plan(tmp_path).candidates[0].node_ids == (1, 2, 3)


def test_quieter_path_can_win_over_shorter_major_road(local_map, tmp_path):
    graph, _ = local_map
    graph.nodes[2].pop("highway")
    graph.edges[1, 2, 0]["length"] = 80
    graph.edges[2, 3, 0].update(length=80, highway="residential", surface="asphalt")
    graph.add_edge(1, 3, length=100, highway="primary")
    candidate = plan(tmp_path).candidates[0]
    assert candidate.node_ids == (1, 2, 3)
    assert candidate.metrics.distance_miles == pytest.approx(160 / 1609.344)


def test_surface_preference_changes_the_direct_path(local_map, tmp_path):
    graph, _ = local_map
    graph.nodes[2].pop("highway")
    graph.edges[1, 2, 0].update(length=100, highway="residential", surface="asphalt")
    graph.edges[2, 3, 0].update(length=100, highway="residential", surface="asphalt")
    graph.add_edge(1, 4, length=120, highway="path", surface="gravel")
    graph.add_edge(4, 3, length=120, highway="path", surface="gravel")
    road = plan(tmp_path, surface=SurfacePreference.ROAD).candidates[0]
    trail = plan(tmp_path, surface=SurfacePreference.TRAIL).candidates[0]
    assert road.node_ids == (1, 2, 3)
    assert trail.node_ids == (1, 4, 3)
    assert road.metrics.trail_fraction == 0
    assert trail.metrics.trail_fraction == 1


def test_no_path_in_the_requested_direction_is_an_actionable_error(local_map, tmp_path):
    with pytest.raises(ValueError, match="No walkable path connects"):
        plan(tmp_path, origin=Coordinate(0, .002), finish=Coordinate(0, 0))


def test_endpoints_snapping_to_the_same_node_are_rejected(local_map, tmp_path):
    with pytest.raises(ValueError, match="same point"):
        plan(tmp_path, finish=Coordinate(0, .00001))


@pytest.mark.parametrize("endpoint", ["start", "finish"])
def test_far_off_network_endpoint_is_not_connected_with_a_fictional_line(local_map, tmp_path, endpoint):
    arguments = {"origin": Coordinate(.02, .001)} if endpoint == "start" else {"finish": Coordinate(.02, .001)}
    with pytest.raises(CoverageError, match=f"The {endpoint} is too far"):
        plan(tmp_path, **arguments)


def test_both_endpoints_must_have_a_common_local_map(local_map, tmp_path):
    with pytest.raises(CoverageError, match="same supported area"):
        plan(tmp_path, finish=Coordinate(1, 1))


def test_empty_local_coverage_does_not_use_a_regional_loop_backend(local_map, tmp_path, monkeypatch):
    monkeypatch.setattr(direct, "areas", lambda: ())
    monkeypatch.setenv("RUNLINE_REMOTE_API", "https://regional.example")
    with pytest.raises(CoverageError, match="same supported area"):
        plan(tmp_path)


def test_cached_source_graph_is_not_mutated(local_map, tmp_path):
    graph, _ = local_map
    before = copy.deepcopy(graph)
    nx.freeze(graph)
    plan(tmp_path)
    assert dict(graph.graph) == dict(before.graph)
    assert list(graph.nodes(data=True)) == list(before.nodes(data=True))
    assert list(graph.edges(keys=True, data=True)) == list(before.edges(keys=True, data=True))


def test_meaningful_snapping_is_reported_without_inventing_a_connector(local_map, tmp_path):
    result = plan(tmp_path, origin=Coordinate(-.0005, 0))
    assert result.candidates[0].coordinates[0] == Coordinate(0, 0)
    assert any("start was moved" in warning for warning in result.warnings)
    assert result.candidates[0].metrics.distance_miles == pytest.approx(200 / 1609.344)
