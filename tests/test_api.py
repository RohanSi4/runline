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

    assert "Your distance. Fewer interruptions." in frontend


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
