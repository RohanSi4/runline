from __future__ import annotations

import math

from runroute.geo import EARTH_RADIUS_METERS, destination, distance_meters
from runroute.models import Coordinate


def _haversine_meters(first: Coordinate, second: Coordinate) -> float:
    lat1, lat2 = math.radians(first.latitude), math.radians(second.latitude)
    delta_lat = lat2 - lat1
    delta_lon = math.radians(second.longitude - first.longitude)
    haversine = (
        math.sin(delta_lat / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin(delta_lon / 2) ** 2
    )
    return 2 * EARTH_RADIUS_METERS * math.asin(math.sqrt(haversine))


def test_destination_is_requested_distance_away() -> None:
    origin = Coordinate(38.0316, -78.5108)

    result = destination(origin, bearing_degrees=73, distance_meters=5_000)

    assert _haversine_meters(origin, result) == pytest.approx(5_000, rel=1e-6)
    assert distance_meters(origin, result) == pytest.approx(5_000, rel=1e-6)


import pytest
