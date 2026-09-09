from __future__ import annotations

from runline.export import export_candidates
from runline.models import Coordinate, RouteCandidate, RouteMetrics, StartCandidate


def test_export_includes_interactive_preview(tmp_path) -> None:
    candidate = RouteCandidate(
        route_id="route-test",
        node_ids=(1, 2, 1),
        coordinates=(Coordinate(38, -78), Coordinate(38.01, -78.01)),
        metrics=RouteMetrics(3, 100, 0, 0, 0.2, 0.1),
        start=StartCandidate("Start here", "origin", Coordinate(38, -78), 0),
        score=1,
    )

    export_candidates([candidate], tmp_path)

    preview = (tmp_path / "preview.html").read_text(encoding="utf-8")
    assert "Runline Preview" in preview
    assert "route-test" in preview
    assert "tile.openstreetmap.org" in preview
    assert (tmp_path / "option-1.gpx").exists()
