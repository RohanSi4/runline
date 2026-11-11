from __future__ import annotations

import json

import pytest

from scripts import project_hosting
from scripts.project_hosting import Settings, projections


def tiles(slug: str, count: int) -> list[dict]:
    # Only the count matters to the projection; no coordinates needed.
    return [{"slug": f"{slug}-{index:03d}", "cells": 1} for index in range(count)]


# "plain" has DC's measured shape, so its mid case must reproduce DC's figures.
# "packed" has twice plain's population per cell, so it counts as dense.
CONFIG = {
    "totals": {"unique_cells": 102_183},
    "metros": [
        {"slug": "plain", "population": 1_000_000, "cells": 93_183, "tiles": tiles("plain", 230)},
        {"slug": "packed", "population": 200_000, "cells": 9_318, "tiles": tiles("packed", 23)},
        {"slug": "spare", "population": 1_000, "cells": 1_000, "tiles": tiles("spare", 2)},
    ],
}
BATCHES = {"batches": [{"name": "one", "cells": 102_000, "metros": ["plain", "packed"]}]}


def test_mid_case_reproduces_the_dc_measurements() -> None:
    result = projections(CONFIG, BATCHES, {}, Settings(cache=16, workers=1))
    plain = next(item for item in result["metros"] if item["name"] == "plain")

    assert plain["tiles"] == 230 and plain["cells"] == 93_183
    assert plain["dense"] is False
    assert plain["graphs_gb"] == {"low": 2.5, "mid": 4.2, "high": 6.3}
    # Low and mid keep DC's pooled share; high pools every planned cell.
    assert plain["pools_gb"] == {"low": 2.3, "mid": 2.3, "high": 3.0}
    assert plain["disk_gb"]["mid"] == 6.5
    # 1 GB OS + 1.5 GB worker + 16 x 0.65 GB tiles.
    assert plain["ram_gb"]["mid"] == pytest.approx(12.9)
    # (24 - 1 - 1.5) / 0.65 = 33.08, and for 1.5x tiles 22.05.
    assert plain["hot_tiles"] == {"low": 22, "mid": 33, "high": 55}


def test_dense_metros_get_the_heavier_high_case() -> None:
    result = projections(CONFIG, BATCHES, {}, Settings(cache=16, workers=1))
    packed = next(item for item in result["metros"] if item["name"] == "packed")

    assert result["assumptions"]["dense_metros"] == ["packed"]
    assert packed["graphs_gb"] == {"low": 0.3, "mid": 0.4, "high": 0.8}
    # 2.0x tiles: 1 + 1.5 + 16 x 1.3.
    assert packed["ram_gb"]["high"] == pytest.approx(23.3)
    assert packed["hot_tiles"]["low"] == 16


def test_batches_sum_tiles_use_recorded_cells_and_take_the_heaviest_factor() -> None:
    result = projections(CONFIG, BATCHES, {"pair": ["plain", "spare"]}, Settings(cache=4, workers=2))
    batch = result["batches"][0]

    assert batch["tiles"] == 253
    assert batch["cells"] == 102_000
    assert batch["graphs_gb"]["mid"] == pytest.approx(4.6)
    assert batch["dense"] is True
    # Two workers each hold their own cache: 1 + 2 x (1.5 + 4 x 0.65 x 2.0).
    assert batch["ram_gb"]["high"] == pytest.approx(14.4)
    # Per worker: (24 - 1 - 2 x 1.5) / 2 / 0.65 = 15.4.
    assert batch["hot_tiles"]["mid"] == 15

    group = result["groups"][0]
    assert group["tiles"] == 232 and group["cells"] == 94_183 and group["dense"] is False
    # "all" totals every metro and, with batches, their recorded cells.
    assert result["all"]["tiles"] == 255
    assert result["all"]["cells"] == 102_000


def test_unknown_metros_are_rejected() -> None:
    with pytest.raises(ValueError, match="nowhere"):
        projections(CONFIG, {"batches": [{"name": "bad", "metros": ["nowhere"]}]}, {}, Settings())


def test_command_writes_markdown_and_json(tmp_path, capsys) -> None:
    metros = tmp_path / "metros.json"
    batches = tmp_path / "batches.json"
    metros.write_text(json.dumps(CONFIG))
    batches.write_text(json.dumps(BATCHES))
    output = tmp_path / "out.json"

    code = project_hosting.main(
        ["--metros", str(metros), "--batches", str(batches), "--group", "pair=plain,spare", "--json", str(output)]
    )

    assert code == 0
    table = capsys.readouterr().out
    assert "| one * | 2 | 253 | 102,000 |" in table
    assert "| pair | 2 | 232 |" in table
    assert json.loads(output.read_text())["all"]["tiles"] == 255


def test_measured_tile_figures_replace_dcs(tmp_path, capsys) -> None:
    metros = tmp_path / "metros.json"
    metros.write_text(json.dumps(CONFIG))
    output = tmp_path / "out.json"

    project_hosting.main(
        ["--metros", str(metros), "--batches", str(tmp_path / "none.json"),
         "--graph-mb-per-tile", "40", "--tile-ram-mb", "1000", "--cache", "10", "--json", str(output)]
    )

    plain = next(item for item in json.loads(output.read_text())["metros"] if item["name"] == "plain")
    assert plain["graphs_gb"]["mid"] == pytest.approx(9.2)  # 230 x 40 MB
    assert plain["ram_gb"]["mid"] == pytest.approx(12.5)  # 1 + 1.5 + 10 x 1.0
    assert plain["hot_tiles"]["mid"] == 21
