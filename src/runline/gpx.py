from __future__ import annotations

from datetime import UTC, datetime
from xml.etree import ElementTree as ET

from .models import RouteCandidate


def candidate_to_gpx(candidate: RouteCandidate, name: str | None = None) -> str:
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
    route = ET.SubElement(root, "rte")
    ET.SubElement(route, "name").text = name or candidate.route_id

    for coordinate in candidate.coordinates:
        ET.SubElement(
            route,
            "rtept",
            {
                "lat": f"{coordinate.latitude:.7f}",
                "lon": f"{coordinate.longitude:.7f}",
            },
        )

    ET.indent(root, space="  ")
    return ET.tostring(root, encoding="unicode", xml_declaration=True)

