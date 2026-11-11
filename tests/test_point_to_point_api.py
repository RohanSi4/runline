import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

import runline.api as api
from runline.coverage import CoverageError
from runline.models import Coordinate, RouteCandidate, RouteMetrics
from runline.planner import PlanResult


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
    with pytest.raises(HTTPException) as error:
        asyncio.run(api.routes(direct_request()))
    assert error.value.status_code == 422
    assert "unavailable" in error.value.detail


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
    monkeypatch.setattr(api, "areas", lambda: (SimpleNamespace(label="Local"),))
    monkeypatch.setattr(api, "combined_area_labels", lambda: ["Local", "Regional"])
    result = asyncio.run(api.capabilities())
    assert result["modes"]["point_to_point"]["areas"] == ["Local"]
    assert result["modes"]["loop"]["areas"] == ["Local", "Regional"]
