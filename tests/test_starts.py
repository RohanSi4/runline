from __future__ import annotations

from shapely.geometry import Point

from runline.models import Coordinate
from runline.starts import _feature_candidate


def test_public_trailhead_becomes_start_candidate() -> None:
    origin = Coordinate(38, -78)

    candidate = _feature_candidate(
        {"information": "trailhead", "name": "Creek Trail"},
        Point(-78.01, 38),
        origin,
        radius_miles=5,
    )

    assert candidate is not None
    assert candidate.label == "Creek Trail"
    assert candidate.kind == "trailhead"


def test_private_parking_is_excluded() -> None:
    candidate = _feature_candidate(
        {"amenity": "parking", "access": "private"},
        Point(-78.01, 38),
        Coordinate(38, -78),
        radius_miles=5,
    )

    assert candidate is None


def test_a_drive_is_never_shorter_than_the_straight_line() -> None:
    """Shipped Charlottesville starts: the reported drive includes the legs onto the roads."""

    from runline.coverage import areas, load_area_drive_graph, load_area_starts
    from runline.geo import distance_meters
    from runline.models import METERS_PER_MILE, Coordinate
    from runline.starts import _road_distance_miles

    area = next(item for item in areas() if item.slug == "charlottesville-va")
    graph = load_area_drive_graph(area)
    entries = load_area_starts(area)
    origin = area.center
    checked = 0
    for entry in entries[:: max(1, len(entries) // 40)]:
        destination = Coordinate(float(entry["latitude"]), float(entry["longitude"]))
        road = _road_distance_miles(graph, origin, destination)
        if road is None:
            continue
        checked += 1
        assert road * METERS_PER_MILE >= distance_meters(origin, destination) - 1e-6
    assert checked >= 20
