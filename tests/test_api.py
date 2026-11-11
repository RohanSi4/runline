from __future__ import annotations

import asyncio
from pathlib import Path
import tempfile

import pytest
from fastapi import HTTPException
import runline.api as api
from runline.api import cache_root, health, index


def test_health_and_frontend_are_served() -> None:
    assert asyncio.run(health()) == {"status": "ok"}
    response = asyncio.run(index())
    frontend = Path(response.path).read_text(encoding="utf-8")

    assert 'id="route-form"' in frontend
    assert 'id="start-run"' in frontend


def test_backend_release_can_be_verified_independently_of_frontend(monkeypatch) -> None:
    monkeypatch.setenv("RUNLINE_BUILD_SHA", "a" * 40)
    monkeypatch.setenv("VERCEL_GIT_COMMIT_SHA", "b" * 40)
    result = asyncio.run(api.version())
    assert result == {"version": api.app.version, "commit": "a" * 40}
    assert asyncio.run(health()) == {"status": "ok"}


def test_unknown_address_returns_422(monkeypatch) -> None:
    def fail(*_args, **_kwargs):
        raise ValueError("could not find a location for 'nowhere'")

    monkeypatch.setattr(api, "geocode_address", fail)
    request = api.RouteRequest(address="nowhere", distance_miles=3)
    with pytest.raises(HTTPException) as error:
        asyncio.run(api.routes(request))
    assert error.value.status_code == 422
    assert "nowhere" in error.value.detail


def test_vercel_cache_uses_writable_tmp(monkeypatch) -> None:
    monkeypatch.setenv("VERCEL", "1")

    assert cache_root().parent == Path(tempfile.gettempdir())


def test_cache_dir_override_wins_over_vercel(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("VERCEL", "1")
    monkeypatch.setenv("RUNLINE_CACHE_DIR", str(tmp_path / "cache"))

    assert cache_root() == tmp_path / "cache"
