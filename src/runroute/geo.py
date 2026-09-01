from __future__ import annotations

import math

from .models import Coordinate


EARTH_RADIUS_METERS = 6_371_008.8


def distance_meters(first: Coordinate, second: Coordinate) -> float:
    first_latitude = math.radians(first.latitude)
    second_latitude = math.radians(second.latitude)
    delta_latitude = second_latitude - first_latitude
    delta_longitude = math.radians(second.longitude - first.longitude)
    haversine = (
        math.sin(delta_latitude / 2) ** 2
        + math.cos(first_latitude)
        * math.cos(second_latitude)
        * math.sin(delta_longitude / 2) ** 2
    )
    return 2 * EARTH_RADIUS_METERS * math.asin(math.sqrt(haversine))


def destination(origin: Coordinate, bearing_degrees: float, distance_meters: float) -> Coordinate:
    angular_distance = distance_meters / EARTH_RADIUS_METERS
    bearing = math.radians(bearing_degrees)
    latitude = math.radians(origin.latitude)
    longitude = math.radians(origin.longitude)

    target_latitude = math.asin(
        math.sin(latitude) * math.cos(angular_distance)
        + math.cos(latitude) * math.sin(angular_distance) * math.cos(bearing)
    )
    target_longitude = longitude + math.atan2(
        math.sin(bearing) * math.sin(angular_distance) * math.cos(latitude),
        math.cos(angular_distance) - math.sin(latitude) * math.sin(target_latitude),
    )
    return Coordinate(math.degrees(target_latitude), math.degrees(target_longitude))
