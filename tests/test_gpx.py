from __future__ import annotations

from xml.etree import ElementTree as ET

from runroute.gpx import candidate_to_gpx
from runroute.models import Coordinate, RouteCandidate, RouteMetrics


def test_gpx_contains_route_points() -> None:
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

    points = root.findall("gpx:rte/gpx:rtept", namespace)
    assert len(points) == 3
    assert points[0].attrib == {"lat": "38.0000000", "lon": "-78.5000000"}
