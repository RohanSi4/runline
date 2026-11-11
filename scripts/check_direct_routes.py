"""Rehearse direct paths using the page's start and a shipped public place."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import xml.etree.ElementTree as ET

import requests

from presentation_check import DefaultStart


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", required=True)
    parser.add_argument("--output", type=Path, default=Path("output/direct-check.json"))
    args = parser.parse_args()
    base = args.api.rstrip("/")
    session = requests.Session()
    page = session.get(base + "/", timeout=30); page.raise_for_status()
    start = DefaultStart(); start.feed(page.text)
    response = session.get(base + "/api/places", timeout=30); response.raise_for_status()
    finish = next(place for place in response.json()["places"] if place["name"] == "The Lawn")
    destination = {key: finish[key] for key in ("latitude", "longitude")}
    report = {"api": base, "cases": []}
    for surface in ("mixed", "road", "trail"):
        response = session.post(base + "/api/routes", json={"mode": "point_to_point", **start.values,
                                "destination": destination, "surface": surface}, timeout=90)
        response.raise_for_status(); payload = response.json()
        assert payload["mode"] == "point_to_point" and payload["source"] == "live"
        assert payload["routes"]
        for route in payload["routes"]:
            feature = route["feature"]; points = feature["geometry"]["coordinates"]
            assert feature["geometry"]["type"] == "LineString" and len(points) >= 2 and points[0] != points[-1]
            assert points[0] == [payload["origin"]["longitude"], payload["origin"]["latitude"]]
            assert points[-1] == [payload["destination"]["longitude"], payload["destination"]["latitude"]]
            assert feature["properties"]["distance_miles"] > 0
            gpx = ET.fromstring(route["gpx"]).findall(".//{*}trkpt")
            assert len(gpx) == len(points)
            assert all(abs(float(p.attrib["lat"]) - lat) < 1e-6 and abs(float(p.attrib["lon"]) - lon) < 1e-6
                       for p, (lon, lat) in zip(gpx, points))
        case = {"surface": surface, "routes": len(payload["routes"]), "miles": [round(r["feature"]["properties"]["distance_miles"], 3) for r in payload["routes"]]}
        report["cases"].append(case); print(case, flush=True)
    for name, body in [("same endpoints", {"mode": "point_to_point", **start.values, "destination": start.values}),
                       ("missing finish", {"mode": "point_to_point", **start.values}),
                       ("outside direct coverage", {"mode": "point_to_point", "latitude": 0, "longitude": 0, "destination": destination})]:
        response = session.post(base + "/api/routes", json=body, timeout=30)
        assert response.status_code == 422 and response.json().get("detail"), name
    report["passed"] = True
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print("Direct paths, GPX and error checks passed", flush=True)


if __name__ == "__main__":
    main()
