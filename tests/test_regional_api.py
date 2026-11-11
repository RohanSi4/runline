from __future__ import annotations

import asyncio

import pytest
import requests
from fastapi import HTTPException

import runline.api as api
from runline.coverage import CoverageError
from runline.models import Coordinate
from runline.planner import PlanResult


class Response:
    def __init__(self, payload, status=200):
        self.payload, self.status_code = payload, status

    def json(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))


@pytest.fixture
def regional(monkeypatch):
    monkeypatch.setenv("RUNLINE_REMOTE_API", "https://regional.example/")

    def outside(*args, **kwargs):
        raise CoverageError("outside local coverage")

    monkeypatch.setattr(api, "plan_routes", outside)


def test_address_outside_local_graph_uses_resolved_origin_and_keeps_preferences(regional, monkeypatch):
    monkeypatch.setattr(api, "geocode_address", lambda *args: Coordinate(0, 0))
    payload = {"origin": {"latitude": 0, "longitude": 0}, "source": "pool",
               "routes": [{"feature": {}, "gpx": "<gpx/>"}], "warnings": []}
    calls = []

    def post(url, **kwargs):
        calls.append((url, kwargs))
        return Response(payload)

    monkeypatch.setattr(api.requests, "post", post)
    request = api.RouteRequest(address="a synthetic start", distance_miles=8, surface="trail", elevation="hilly")
    assert asyncio.run(api.routes(request)) == payload
    assert calls[0][0] == "https://regional.example/api/routes"
    assert calls[0][1]["json"] == {"latitude": 0, "longitude": 0, "distance_miles": 8,
                                    "drive_radius_miles": 0, "surface": "trail",
                                    "elevation": "hilly", "result_count": 3}


def test_regional_coverage_is_combined_without_duplicates(regional, monkeypatch):
    monkeypatch.setattr(api, "area_labels", lambda: ("Local",))
    monkeypatch.setattr(api.requests, "get", lambda *args, **kwargs: Response({"areas": ["Local", "Regional"]}))
    assert asyncio.run(api.supported_areas()) == {"areas": ["Local", "Regional"]}


def test_regional_outage_preserves_local_coverage(regional, monkeypatch):
    monkeypatch.setattr(api, "area_labels", lambda: ("Local",))

    def fail(*args, **kwargs):
        raise requests.Timeout()

    monkeypatch.setattr(api.requests, "get", fail)
    assert asyncio.run(api.supported_areas()) == {"areas": ["Local"]}


@pytest.mark.parametrize("response", [Response(ValueError("HTML"), 502), Response({"routes": []}), Response({}, 503)])
def test_bad_regional_response_is_an_explained_503(regional, monkeypatch, response):
    monkeypatch.setattr(api.requests, "post", lambda *args, **kwargs: response)
    with pytest.raises(HTTPException) as error:
        asyncio.run(api.routes(api.RouteRequest(latitude=0, longitude=0, distance_miles=5)))
    assert error.value.status_code == 503
    assert "regional" in error.value.detail


def test_unsupported_start_preserves_regional_error(regional, monkeypatch):
    monkeypatch.setattr(api.requests, "post", lambda *args, **kwargs: Response({"detail": "Outside both regions"}, 422))
    with pytest.raises(HTTPException) as error:
        asyncio.run(api.routes(api.RouteRequest(latitude=0, longitude=0, distance_miles=5)))
    assert error.value.status_code == 422
    assert error.value.detail == "Outside both regions"


def test_local_planning_is_not_replaced_by_regional_backend(regional, monkeypatch):
    monkeypatch.setattr(api, "plan_routes", lambda *args, **kwargs: PlanResult([], 0, 0, ()))

    def unexpected(*args, **kwargs):
        pytest.fail("local result should not call the regional service")

    monkeypatch.setattr(api.requests, "post", unexpected)
    with pytest.raises(HTTPException) as error:
        asyncio.run(api.routes(api.RouteRequest(latitude=0, longitude=0, distance_miles=5)))
    assert error.value.status_code == 422
    assert "No viable loops" in error.value.detail


def test_standalone_backend_never_forwards(monkeypatch):
    monkeypatch.delenv("RUNLINE_REMOTE_API", raising=False)

    def outside(*args, **kwargs):
        raise CoverageError("local coverage only")

    monkeypatch.setattr(api, "plan_routes", outside)
    with pytest.raises(HTTPException) as error:
        asyncio.run(api.routes(api.RouteRequest(latitude=0, longitude=0, distance_miles=5)))
    assert error.value.status_code == 422
    assert error.value.detail == "local coverage only"
