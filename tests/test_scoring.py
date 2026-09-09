from __future__ import annotations

from runline.models import (
    Coordinate,
    ElevationPreference,
    RouteCandidate,
    RouteMetrics,
    RoutePreferences,
)
from runline.scoring import rank_candidates


def _candidate(route_id: str, *, signals: int = 0, gain: float = 100) -> RouteCandidate:
    return RouteCandidate(
        route_id=route_id,
        node_ids=(1, 2, 1),
        coordinates=(Coordinate(38, -78), Coordinate(38.01, -78.01)),
        metrics=RouteMetrics(
            distance_miles=5,
            elevation_gain_feet=gain,
            traffic_signal_events=signals,
            major_crossing_events=0,
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
