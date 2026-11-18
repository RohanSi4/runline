"""Live answers that double back say so instead of passing silently."""

from __future__ import annotations

from runline.models import RouteCandidate, RouteMetrics
from runline.planner import awkward_notice
from runline.scoring import is_awkward


def _route(repeated: float = 0.02, compactness: float = 0.5) -> RouteCandidate:
    metrics = RouteMetrics(3.0, 100, 0, 1, 0.5, repeated, compactness=compactness)
    return RouteCandidate("r", (1, 2, 1), (), metrics)


def test_round_routes_get_no_notice() -> None:
    assert awkward_notice([_route(), _route(), _route()]) is None
    assert awkward_notice([]) is None


def test_awkward_means_out_and_back_or_over_ten_percent_repeated() -> None:
    assert is_awkward(_route(repeated=0.11).metrics)
    assert is_awkward(_route(compactness=0.13).metrics)
    assert not is_awkward(_route(repeated=0.10, compactness=0.2).metrics)


def test_one_awkward_route_is_counted() -> None:
    notice = awkward_notice([_route(), _route(), _route(repeated=0.23)])
    assert notice.startswith("1 of these routes runs out and back or repeats more than 10% of its distance")


def test_some_awkward_routes_are_counted() -> None:
    notice = awkward_notice([_route(), _route(compactness=0.1), _route(repeated=0.3)])
    assert notice.startswith("2 of these routes run out and back or repeat more than 10% of their distance")


def test_an_answer_of_only_awkward_routes_says_no_rounder_loop_was_found() -> None:
    notice = awkward_notice([_route(repeated=0.23, compactness=0.13)] * 3)
    assert notice.startswith("No rounder loop of this length was found from this start")
