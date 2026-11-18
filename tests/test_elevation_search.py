"""'Flat' steers the path search away from climbs; other settings search as before."""

from __future__ import annotations

from runline.models import ElevationPreference, RoutePreferences
from runline.osm import FLAT_CLIMB_COST, _edge_preference_cost


EDGE = {"length": 100.0, "highway": "residential"}
LOW, HIGH = {"elevation": 100.0}, {"elevation": 105.0}


def _cost(elevation: ElevationPreference, source, destination) -> float:
    return _edge_preference_cost(dict(EDGE), destination, RoutePreferences(5, elevation=elevation), source)


def test_flat_pays_for_climbing_but_not_for_descending() -> None:
    assert _cost(ElevationPreference.FLAT, LOW, HIGH) == 100.0 + 5.0 * FLAT_CLIMB_COST
    assert _cost(ElevationPreference.FLAT, HIGH, LOW) == 100.0


def test_balanced_and_hilly_search_on_distance_as_before() -> None:
    for elevation in (ElevationPreference.BALANCED, ElevationPreference.HILLY):
        assert _cost(elevation, LOW, HIGH) == _cost(elevation, HIGH, LOW) == 100.0


def test_missing_elevation_adds_nothing() -> None:
    assert _cost(ElevationPreference.FLAT, {}, HIGH) == _cost(ElevationPreference.FLAT, LOW, {}) == 100.0
