from __future__ import annotations

import asyncio
from pathlib import Path
import tempfile

import pytest
from fastapi import HTTPException
import runroute.api as api
from runroute.api import cache_root, demo, health, index
from runroute.export import export_candidates
from runroute.models import Coordinate, RouteCandidate, RouteMetrics


def test_health_and_frontend_are_served() -> None:
    assert asyncio.run(health()) == {"status": "ok"}
    response = asyncio.run(index())
    frontend = Path(response.path).read_text(encoding="utf-8")

    assert "Your distance. Fewer interruptions." in frontend


def test_demo_uses_generated_fixture(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(api, "PROJECT_ROOT", tmp_path)
    candidates = [
        RouteCandidate(
            route_id=f"synthetic-{index}",
            node_ids=(1, 2, 1),
            coordinates=(Coordinate(0, 0), Coordinate(0, 0.01), Coordinate(0, 0)),
            metrics=RouteMetrics(3, 100, 0, 0, 0.2, 0.1),
            score=index,
        )
        for index in range(3)
    ]
    export_candidates(candidates, tmp_path / "output" / "legacy-3-drive-1")
    result = asyncio.run(demo())
    assert len(result["routes"]) == 3
    assert all("<gpx" in route["gpx"] for route in result["routes"])


def test_demo_without_generated_routes_returns_404(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(api, "PROJECT_ROOT", tmp_path)
    with pytest.raises(HTTPException) as error:
        asyncio.run(demo())
    assert error.value.status_code == 404


def test_vercel_cache_uses_writable_tmp(monkeypatch) -> None:
    monkeypatch.setenv("VERCEL", "1")

    assert cache_root().parent == Path(tempfile.gettempdir())
