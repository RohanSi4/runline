from __future__ import annotations

from runline.models import (
    Coordinate,
    ElevationPreference,
    RouteCandidate,
    RouteMetrics,
    RoutePreferences,
)
from runline.scoring import rank_candidates


def _candidate(route_id: str, *, signals: int = 0, crossings: int = 0, gain: float = 100, distance: float = 5) -> RouteCandidate:
    return RouteCandidate(
        route_id=route_id,
        node_ids=(1, 2, 1),
        coordinates=(Coordinate(38, -78), Coordinate(38.01, -78.01)),
        metrics=RouteMetrics(
            distance_miles=distance,
            elevation_gain_feet=gain,
            traffic_signal_events=signals,
            major_crossing_events=crossings,
            trail_fraction=0.5,
            repeated_fraction=0,
        ),
    )


def test_fewer_signals_win_when_other_metrics_match() -> None:
    quiet = _candidate("quiet", signals=0)
    interrupted = _candidate("interrupted", signals=3)

    ranked = rank_candidates(
        [interrupted, quiet], RoutePreferences(target_distance_miles=5)
    )

    assert [candidate.route_id for candidate in ranked] == ["quiet", "interrupted"]


def test_elevation_preference_changes_ranking() -> None:
    flat = _candidate("flat", gain=50)
    hilly = _candidate("hilly", gain=500)

    flat_ranked = rank_candidates(
        [flat, hilly],
        RoutePreferences(target_distance_miles=5, elevation=ElevationPreference.FLAT),
    )
    hilly_ranked = rank_candidates(
        [flat, hilly],
        RoutePreferences(target_distance_miles=5, elevation=ElevationPreference.HILLY),
    )

    assert flat_ranked[0].route_id == "flat"
    assert hilly_ranked[0].route_id == "hilly"


def test_feasible_distance_band_takes_priority() -> None:
    feasible = _candidate("feasible", signals=8, distance=5.24)
    missed = _candidate("missed", distance=5.26)

    assert rank_candidates([missed, feasible], RoutePreferences(5))[0] is feasible


def test_closest_route_is_fallback_when_none_are_feasible() -> None:
    near = _candidate("near", signals=8, distance=5.3)
    far = _candidate("far", distance=5.4)

    assert rank_candidates([far, near], RoutePreferences(5))[0] is near


def test_two_signals_do_not_beat_three_fewer_crossings() -> None:
    quiet = _candidate("quiet", crossings=5, distance=8)
    interrupted = _candidate("interrupted", signals=2, crossings=2, distance=8)

    assert rank_candidates([interrupted, quiet], RoutePreferences(8))[0] is quiet
