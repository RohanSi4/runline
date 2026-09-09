from __future__ import annotations

from dataclasses import dataclass

from .models import ElevationPreference, RouteCandidate, RoutePreferences, SurfacePreference


@dataclass(frozen=True)
class ScoreWeights:
    distance: float = 7.0
    traffic_signals: float = 6.0
    major_crossings: float = 4.0
    surface: float = 2.5
    elevation: float = 1.5
    repeated_segments: float = 5.0
    driving: float = 0.35


def _normalized(value: float, minimum: float, maximum: float) -> float:
    if maximum <= minimum:
        return 0.5
    return (value - minimum) / (maximum - minimum)


def _surface_penalty(surface: SurfacePreference, trail_fraction: float) -> float:
    if surface is SurfacePreference.ROAD:
        return trail_fraction
    if surface is SurfacePreference.TRAIL:
        return 1.0 - trail_fraction
    return abs(trail_fraction - 0.5) * 0.6


def _elevation_penalty(
    preference: ElevationPreference,
    gain_feet: float,
    minimum_gain: float,
    maximum_gain: float,
    coverage: float,
) -> float:
    if coverage < 0.95:
        return 0.5
    position = _normalized(gain_feet, minimum_gain, maximum_gain)
    if preference is ElevationPreference.FLAT:
        return position
    if preference is ElevationPreference.HILLY:
        return 1.0 - position
    return abs(position - 0.5) * 2.0


def rank_candidates(
    candidates: list[RouteCandidate],
    preferences: RoutePreferences,
    weights: ScoreWeights | None = None,
) -> list[RouteCandidate]:
    """Rank candidates using transparent, runner-facing score components.

    Traffic controls intentionally outweigh surface and elevation. Distance remains
    the strongest term, while repeated segments are allowed but discouraged.
    """

    if not candidates:
        return []

    weights = weights or ScoreWeights()
    gains = [candidate.metrics.elevation_gain_feet for candidate in candidates]
    minimum_gain, maximum_gain = min(gains), max(gains)

    for candidate in candidates:
        metrics = candidate.metrics
        distance_error = abs(metrics.distance_miles - preferences.target_distance_miles)
        distance_penalty = distance_error / preferences.distance_tolerance_miles
        signal_penalty = metrics.traffic_signal_events / max(metrics.distance_miles, 1)
        crossing_penalty = metrics.major_crossing_events / max(metrics.distance_miles, 1)
        surface_penalty = _surface_penalty(preferences.surface, metrics.trail_fraction)
        elevation_penalty = _elevation_penalty(
            preferences.elevation,
            metrics.elevation_gain_feet,
            minimum_gain,
            maximum_gain,
            metrics.elevation_coverage,
        )

        components = {
            "distance": weights.distance * distance_penalty,
            "traffic_signals": weights.traffic_signals * signal_penalty,
            "major_crossings": weights.major_crossings * crossing_penalty,
            "surface": weights.surface * surface_penalty,
            "elevation": weights.elevation * elevation_penalty,
            "repeated_segments": weights.repeated_segments * metrics.repeated_fraction,
            "driving": weights.driving * metrics.drive_distance_miles,
        }
        candidate.score_components = components
        candidate.score = sum(components.values())

    return sorted(candidates, key=lambda candidate: candidate.score)
