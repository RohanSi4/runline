import asyncio

import pytest
import requests
from fastapi import HTTPException
from pydantic import ValidationError

import runline.api as api
from runline.coverage import CoverageError
from runline.models import Coordinate, RouteCandidate, RouteMetrics
from runline.planner import PlanResult


class Response:
    def __init__(self, payload, status=200):
        self.payload, self.status_code = payload, status

    def json(self):
        return self.payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))


@pytest.fixture(autouse=True)
def fresh_remote_capabilities():
    api._remote_direct.clear()
    yield
    api._remote_direct.clear()


def remote_capabilities(direct_areas):
    return Response({"modes": {"loop": {"areas": ["Regional"]}, "point_to_point": {"areas": direct_areas}}})


def direct_request(**kwargs):
    return api.RouteRequest(mode="point_to_point", latitude=0, longitude=0,
                            destination={"latitude": 0.001, "longitude": 0.002}, **kwargs)


def test_direct_api_returns_open_geometry_and_actual_finish(monkeypatch):
    start, finish = Coordinate(0, 0), Coordinate(0.001, 0.002)
    candidate = RouteCandidate("path", (1, 2), (start, finish), RouteMetrics(0.15, 10, 0, 0, 0, 0), score=100)
    seen = []

    def plan(origin, destination, preferences, **kwargs):
        seen.append((origin, destination, preferences))
        return PlanResult([candidate], 0, 0, (), "live")

    monkeypatch.setattr(api, "plan_point_to_point", plan)
    result = asyncio.run(api.routes(direct_request(surface="trail")))
    assert result["mode"] == "point_to_point"
    assert result["origin"] == {"latitude": 0, "longitude": 0}
    assert result["destination"] == {"latitude": 0.001, "longitude": 0.002}
    assert result["routes"][0]["feature"]["geometry"]["coordinates"] == [[0, 0], [0.002, 0.001]]
    assert "<trkpt" in result["routes"][0]["gpx"]
    assert seen[0][2].surface.value == "trail"


def test_direct_addresses_resolve_each_endpoint_once(monkeypatch):
    looked_up = []

    def geocode(address, *args):
        looked_up.append(address)
        return Coordinate(0, 0) if len(looked_up) == 1 else Coordinate(0.001, 0.002)

    def stop(*args, **kwargs):
        raise ValueError("synthetic planned stop")

    monkeypatch.setattr(api, "geocode_address", geocode)
    monkeypatch.setattr(api, "plan_point_to_point", stop)
    with pytest.raises(HTTPException) as error:
        asyncio.run(api.routes(api.RouteRequest(mode="point_to_point", address="Synthetic A", destination={"address": "Synthetic B"})))
    assert error.value.status_code == 422
    assert looked_up == ["Synthetic A", "Synthetic B"]


def test_direct_unsupported_coverage_never_calls_legacy_regional_api(monkeypatch):
    monkeypatch.setenv("RUNLINE_REMOTE_API", "https://regional.example")

    def outside(*args, **kwargs):
        raise CoverageError("Direct routes are unavailable in this area")

    def unexpected(*args, **kwargs):
        pytest.fail("legacy backend must never receive a direct request")

    monkeypatch.setattr(api, "plan_point_to_point", outside)
    monkeypatch.setattr(api.requests, "post", unexpected)
    # A legacy backend has no /api/capabilities at all.
    monkeypatch.setattr(api.requests, "get", lambda *args, **kwargs: Response({"detail": "Not Found"}, 404))
    with pytest.raises(HTTPException) as error:
        asyncio.run(api.routes(direct_request()))
    assert error.value.status_code == 422
    assert "unavailable" in error.value.detail


@pytest.mark.parametrize("answer", [
    remote_capabilities([]),
    Response({"modes": {"loop": {"areas": ["Regional"]}}}),
    Response(["not", "capabilities"]),
    requests.Timeout(),
])
def test_direct_is_not_forwarded_unless_the_remote_reports_direct_support(monkeypatch, answer):
    monkeypatch.setenv("RUNLINE_REMOTE_API", "https://regional.example")

    def outside(*args, **kwargs):
        raise CoverageError("Choose a start and finish inside the same supported area.")

    def get(*args, **kwargs):
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(api, "plan_point_to_point", outside)
    monkeypatch.setattr(api.requests, "get", get)
    monkeypatch.setattr(api.requests, "post", lambda *args, **kwargs: pytest.fail("must not forward"))
    with pytest.raises(HTTPException) as error:
        asyncio.run(api.routes(direct_request()))
    assert error.value.status_code == 422
    assert "same supported area" in error.value.detail


def test_direct_is_forwarded_with_its_destination_to_a_remote_reporting_direct_support(monkeypatch):
    monkeypatch.setenv("RUNLINE_REMOTE_API", "https://regional.example/")
    gets, posts = [], []
    payload = {"mode": "point_to_point", "origin": {"latitude": 0, "longitude": 0},
               "destination": {"latitude": 0.001, "longitude": 0.002}, "source": "live",
               "routes": [{"feature": {}, "gpx": "<gpx/>"}], "warnings": []}

    def get(url, **kwargs):
        gets.append((url, kwargs["timeout"]))
        return remote_capabilities(["Regional"])

    def post(url, **kwargs):
        posts.append((url, kwargs["json"]))
        return Response(payload)

    def outside(*args, **kwargs):
        raise CoverageError("outside local direct coverage")

    monkeypatch.setattr(api, "plan_point_to_point", outside)
    monkeypatch.setattr(api.requests, "get", get)
    monkeypatch.setattr(api.requests, "post", post)
    assert asyncio.run(api.routes(direct_request(surface="road"))) == payload
    assert asyncio.run(api.routes(direct_request(surface="road"))) == payload
    # The capability check is cached and short.
    assert gets == [("https://regional.example/api/capabilities", (2, 3))]
    assert posts[0][0] == "https://regional.example/api/routes"
    assert posts[0][1] == {"mode": "point_to_point", "latitude": 0, "longitude": 0,
                           "destination": {"latitude": 0.001, "longitude": 0.002},
                           "distance_miles": None, "drive_radius_miles": 0, "surface": "road",
                           "elevation": "balanced", "result_count": 3}


def test_a_remote_that_answers_a_direct_request_with_a_loop_is_a_503(monkeypatch):
    monkeypatch.setenv("RUNLINE_REMOTE_API", "https://regional.example")
    loop = {"origin": {"latitude": 0, "longitude": 0}, "source": "pool",
            "routes": [{"feature": {}, "gpx": "<gpx/>"}], "warnings": []}

    def outside(*args, **kwargs):
        raise CoverageError("outside local direct coverage")

    monkeypatch.setattr(api, "plan_point_to_point", outside)
    monkeypatch.setattr(api.requests, "get", lambda *args, **kwargs: remote_capabilities(["Regional"]))
    monkeypatch.setattr(api.requests, "post", lambda *args, **kwargs: Response(loop))
    with pytest.raises(HTTPException) as error:
        asyncio.run(api.routes(direct_request()))
    assert error.value.status_code == 503


def test_a_failed_capability_check_is_retried_only_after_a_pause(monkeypatch):
    monkeypatch.setenv("RUNLINE_REMOTE_API", "https://regional.example")
    calls = []

    def get(*args, **kwargs):
        calls.append(1)
        raise requests.ConnectionError()

    clock = [1000.0]
    monkeypatch.setattr(api.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(api.requests, "get", get)
    assert api.remote_direct_areas() == () and api.remote_direct_areas() == ()
    assert len(calls) == 1
    clock[0] += api.REMOTE_CAPABILITIES_RETRY_SECONDS + 1
    api.remote_direct_areas()
    assert len(calls) == 2


@pytest.mark.parametrize("kwargs", [{"distance_miles": 5}, {"drive_radius_miles": 1}, {"elevation": "hilly"}])
def test_direct_mode_rejects_loop_only_preferences(kwargs):
    with pytest.raises(ValidationError):
        direct_request(**kwargs)


def test_mode_validation_requires_distance_for_loop_and_destination_for_direct():
    with pytest.raises(ValidationError):
        api.RouteRequest(latitude=0, longitude=0)
    with pytest.raises(ValidationError):
        api.RouteRequest(mode="point_to_point", latitude=0, longitude=0)


def test_capabilities_separate_direct_coverage_from_regional_loops(monkeypatch):
    monkeypatch.setenv("RUNLINE_REMOTE_API", "https://regional.example")
    monkeypatch.setattr(api, "direct_area_labels", lambda: ("Local",))
    monkeypatch.setattr(api, "combined_area_labels", lambda: ["Local", "Regional"])
    monkeypatch.setattr(api.requests, "get", lambda *args, **kwargs: remote_capabilities([]))
    result = asyncio.run(api.capabilities())
    assert result["modes"]["point_to_point"]["areas"] == ["Local"]
    assert result["modes"]["loop"]["areas"] == ["Local", "Regional"]


def test_capabilities_include_a_remote_that_reports_direct_support(monkeypatch):
    monkeypatch.setenv("RUNLINE_REMOTE_API", "https://regional.example")
    monkeypatch.setattr(api, "direct_area_labels", lambda: ("Local",))
    monkeypatch.setattr(api, "combined_area_labels", lambda: ["Local", "Regional"])
    monkeypatch.setattr(api.requests, "get", lambda *args, **kwargs: remote_capabilities(["Local", "Regional"]))
    result = asyncio.run(api.capabilities())
    assert result["modes"]["point_to_point"]["areas"] == ["Local", "Regional"]
