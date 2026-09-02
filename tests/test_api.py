from __future__ import annotations

import asyncio
from pathlib import Path
import tempfile

from runroute.api import cache_root, demo, health, index


def test_health_and_frontend_are_served() -> None:
    assert asyncio.run(health()) == {"status": "ok"}
    response = asyncio.run(index())
    frontend = Path(response.path).read_text(encoding="utf-8")

    assert "Your distance. Fewer interruptions." in frontend
    assert len(asyncio.run(demo())["routes"]) == 3


def test_vercel_cache_uses_writable_tmp(monkeypatch) -> None:
    monkeypatch.setenv("VERCEL", "1")

    assert cache_root().parent == Path(tempfile.gettempdir())
