from __future__ import annotations

from xml.etree import ElementTree as ET

import pytest

from runline.export import export_candidates
from runline.gpx import candidate_to_gpx
from runline.models import Coordinate, RouteCandidate, RouteMetrics


def test_gpx_contains_track_points(tmp_path) -> None:
    candidate = RouteCandidate(
        route_id="route-test",
        node_ids=(1, 2, 1),
        coordinates=(
            Coordinate(38.0, -78.5),
            Coordinate(38.01, -78.49),
            Coordinate(38.0, -78.5),
        ),
        metrics=RouteMetrics(
            distance_miles=3,
            elevation_gain_feet=100,
            traffic_signal_events=0,
            major_crossing_events=0,
            trail_fraction=0,
            repeated_fraction=0,
        ),
    )

    root = ET.fromstring(candidate_to_gpx(candidate))
    namespace = {"gpx": "http://www.topografix.com/GPX/1/1"}

    assert root.find("gpx:rte", namespace) is None
    assert root.findtext("gpx:trk/gpx:name", namespaces=namespace) == "route-test"
    points = root.findall("gpx:trk/gpx:trkseg/gpx:trkpt", namespace)
    assert len(points) == 3
    assert points[0].attrib == {"lat": "38.0000000", "lon": "-78.5000000"}
    assert points[-1].attrib == points[0].attrib
    assert all(point.find("gpx:ele", namespace) is None for point in points)

    export_candidates([candidate], tmp_path)
    exported = ET.parse(tmp_path / "option-1.gpx").getroot()
    assert exported.find("gpx:rte", namespace) is None
    assert exported.findtext("gpx:trk/gpx:name", namespaces=namespace) == "Running loop option 1"
    assert len(exported.findall("gpx:trk/gpx:trkseg/gpx:trkpt", namespace)) == 3


def test_gpx_exports_available_elevations_in_meters(tmp_path) -> None:
    candidate = RouteCandidate(
        route_id="with-elevation",
        node_ids=(1, 2, 3, 4),
        coordinates=tuple(Coordinate(38 + i * 0.01, -78) for i in range(4)),
        metrics=RouteMetrics(3, 100, 0, 0, 0, 0),
    )
    elevations = (125.5, None, 0.0, -12.25)
    namespace = {"gpx": "http://www.topografix.com/GPX/1/1"}
    root = ET.fromstring(candidate_to_gpx(candidate, elevations_meters=elevations))
    points = root.findall("gpx:trk/gpx:trkseg/gpx:trkpt", namespace)
    assert len(points) == 4
    assert [point.findtext("gpx:ele", namespaces=namespace) for point in points] == [
        "125.5", None, "0.0", "-12.25"
    ]

    export_candidates(
        [candidate], tmp_path, elevations_meters={candidate.route_id: elevations}
    )
    exported = ET.parse(tmp_path / "option-1.gpx").getroot()
    assert [element.text for element in exported.findall(".//gpx:ele", namespace)] == [
        "125.5", "0.0", "-12.25"
    ]


@pytest.mark.parametrize("elevations", [(), (1.0, 2.0, 3.0)])
def test_gpx_rejects_misaligned_elevations(elevations) -> None:
    candidate = RouteCandidate(
        route_id="route-test",
        node_ids=(1, 2),
        coordinates=(Coordinate(38, -78), Coordinate(38.01, -78)),
        metrics=RouteMetrics(3, 100, 0, 0, 0, 0),
    )
    with pytest.raises(ValueError, match="elevations_meters.*coordinates"):
        candidate_to_gpx(candidate, elevations_meters=elevations)
