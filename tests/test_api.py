from __future__ import annotations

import asyncio
from pathlib import Path

from runroute.api import demo, health, index


def test_health_and_frontend_are_served() -> None:
    assert asyncio.run(health()) == {"status": "ok"}
    response = asyncio.run(index())
    frontend = Path(response.path).read_text(encoding="utf-8")

    assert "Your distance. Fewer interruptions." in frontend
    assert len(asyncio.run(demo())["routes"]) == 3
