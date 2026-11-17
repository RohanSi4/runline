"""The deployed-validation checks must accept a correct response and catch broken ones."""

import copy

from runline.export import candidate_feature
from runline.gpx import candidate_to_gpx
from runline.models import Coordinate, RouteCandidate, RouteMetrics, RoutePreferences
from runline.scoring import rank_candidates
from scripts.validate_deployed import analyze_loop, p95


def payload(distance=3.1):
    candidate = RouteCandidate(
        "example", (1, 2, 3, 1),
        (Coordinate(10, 20), Coordinate(10.01, 20), Coordinate(10, 20.01), Coordinate(10, 20)),
        RouteMetrics(distance, 120, 2, 1, 0.3, 0.02, compactness=0.3),
    )
    rank_candidates([candidate], RoutePreferences(target_distance_miles=3))
    return {"source": "pool", "origin": {"latitude": 10, "longitude": 20},
            "routes": [{"feature": candidate_feature(candidate), "gpx": candidate_to_gpx(candidate)}]}


def test_matching_response_passes_and_scores_recompute():
    result = analyze_loop(payload(), 3, "mixed", (10, 20))
    assert result["passed"], result["problems"]
    assert result["score_mismatch_count"] == 0
    assert result["routes"][0]["within_5pct"] and result["routes"][0]["gpx_matches"]


def test_open_loop_wrong_gpx_and_altered_score_are_reported():
    data = payload()
    data["routes"][0]["feature"]["geometry"]["coordinates"][-1] = [20.001, 10]
    result = analyze_loop(data, 3, "mixed", (10, 20))
    assert not result["passed"]
    assert any("first point" in p for p in result["problems"])
    assert any("GPX" in p for p in result["problems"])
    data = payload()
    data["routes"][0]["feature"]["properties"]["score_components"]["traffic_signals"] += 1
    assert analyze_loop(data, 3, "mixed", (10, 20))["score_mismatch_count"] == 1


def test_out_of_band_top_route_fails():
    result = analyze_loop(payload(distance=3.4), 3, "mixed", (10, 20))
    assert not result["passed"] and not result["top_in_band"]


def test_nearest_rank_p95():
    assert p95([1, 2, 3, 4, 5, 6, 7, 8, 9, 10]) == 10
    assert p95(list(range(1, 21))) == 19
