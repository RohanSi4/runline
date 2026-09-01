from __future__ import annotations

import json
from pathlib import Path


def test_public_benchmarks_do_not_contain_addresses_or_coordinates() -> None:
    path = Path(__file__).parents[1] / "config" / "benchmarks.public.json"
    benchmark = json.loads(path.read_text(encoding="utf-8"))

    for origin in benchmark["origins"]:
        assert "address" not in origin
        assert "latitude" not in origin
        assert "longitude" not in origin
