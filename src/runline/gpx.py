from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from xml.etree import ElementTree as ET

from .models import RouteCandidate


def candidate_to_gpx(
    candidate: RouteCandidate,
    name: str | None = None,
    *,
    elevations_meters: Sequence[float | None] | None = None,
) -> str:
    """Export a track with optional elevations aligned to coordinates, not node IDs.

    Use None for points without an elevation measurement.
    """
    if elevations_meters is not None and len(elevations_meters) != len(candidate.coordinates):
        raise ValueError("elevations_meters must have the same length as coordinates")
    root = ET.Element(
        "gpx",
        {
            "version": "1.1",
            "creator": "runline",
            "xmlns": "http://www.topografix.com/GPX/1/1",
        },
    )
    metadata = ET.SubElement(root, "metadata")
    ET.SubElement(metadata, "time").text = datetime.now(UTC).isoformat()
    track = ET.SubElement(root, "trk")
    ET.SubElement(track, "name").text = name or candidate.route_id
    segment = ET.SubElement(track, "trkseg")

    for index, coordinate in enumerate(candidate.coordinates):
        point = ET.SubElement(
            segment,
            "trkpt",
            {
                "lat": f"{coordinate.latitude:.7f}",
                "lon": f"{coordinate.longitude:.7f}",
            },
        )
        if elevations_meters is not None and elevations_meters[index] is not None:
            ET.SubElement(point, "ele").text = str(elevations_meters[index])

    ET.indent(root, space="  ")
    return ET.tostring(root, encoding="unicode", xml_declaration=True)

