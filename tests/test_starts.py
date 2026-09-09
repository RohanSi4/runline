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
