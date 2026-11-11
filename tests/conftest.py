from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def no_local_pools(monkeypatch, tmp_path_factory) -> None:
    """Tests plan live unless they set up a pool: a local pools/ must not change results."""

    monkeypatch.setenv("RUNLINE_POOLS_DIR", str(tmp_path_factory.getbasetemp() / "no-pools"))
    monkeypatch.setenv("RUNLINE_METROS_DIR", str(tmp_path_factory.getbasetemp() / "no-metros"))
    # Tile graphs are read from local files only unless a test turns fetching on.
    monkeypatch.delenv("RUNLINE_TILE_GRAPHS_URL", raising=False)
