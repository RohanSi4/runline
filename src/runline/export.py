from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict
from pathlib import Path

from .gpx import candidate_to_gpx
from .models import RouteCandidate
from .preview import preview_html


def _start(candidate: RouteCandidate) -> dict | None:
    if candidate.start is None:
        return None
    return asdict(candidate.start)


def candidate_feature(candidate: RouteCandidate) -> dict:
    return {
        "type": "Feature",
        "properties": {
            "route_id": candidate.route_id,
            "score": candidate.score,
            **asdict(candidate.metrics),
            "score_components": candidate.score_components,
            "start": _start(candidate),
        },
        "geometry": {
            "type": "LineString",
            "coordinates": [
                [coordinate.longitude, coordinate.latitude]
                for coordinate in candidate.coordinates
            ],
        },
    }


def export_candidates(
    candidates: list[RouteCandidate],
    output_directory: Path,
    *,
    elevations_meters: Mapping[str, Sequence[float | None]] | None = None,
) -> None:
    """Export candidates with optional GPX elevations keyed by route_id.

    Each sequence aligns with that candidate's coordinates; missing routes or
    None entries omit elevation for the corresponding track points.
    """
    output_directory.mkdir(parents=True, exist_ok=True)
    summary = []
    features = []
    for index, candidate in enumerate(candidates, start=1):
        public_id = f"option-{index}"
        summary.append(
            {
                "option": index,
                "score": candidate.score,
                "metrics": asdict(candidate.metrics),
                "score_components": candidate.score_components,
                "start": _start(candidate),
            }
        )
        features.append(candidate_feature(candidate))
        (output_directory / f"{public_id}.gpx").write_text(
            candidate_to_gpx(
                candidate,
                name=f"Running loop option {index}",
                elevations_meters=(
                    elevations_meters.get(candidate.route_id)
                    if elevations_meters is not None
                    else None
                ),
            ),
            encoding="utf-8",
        )

    (output_directory / "routes.json").write_text(
        json.dumps(summary, indent=2) + "\n", encoding="utf-8"
    )
    feature_collection = {"type": "FeatureCollection", "features": features}
    (output_directory / "routes.geojson").write_text(
        json.dumps(feature_collection, indent=2) + "\n",
        encoding="utf-8",
    )
    (output_directory / "preview.html").write_text(
        preview_html(feature_collection), encoding="utf-8"
    )
