"""Rehearse the public app's API and GPX exports, saving a concise report.

Starts come from the shipped manifest and the frontend's default start.
Captured routes belong in gitignored output/, never in tracked fixtures.

    python scripts/presentation_check.py --api https://runline-nine.vercel.app \
        --require-pool --capture --output output/presentation/live-check.json
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import sys
import time
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]


class DefaultStart(HTMLParser):
    def __init__(self):
        super().__init__()
        self.values = {}
        self.assets = []

    def handle_starttag(self, tag, attrs):
        data = dict(attrs)
        if tag == "input" and data.get("id") in ("latitude", "longitude"):
            self.values[data["id"]] = float(data["value"])
        asset = data.get("src") if tag == "script" else data.get("href") if tag == "link" else None
        if asset and asset.startswith("/assets/"):
            self.assets.append(asset)


def check_routes(payload: dict, target: float) -> dict:
    routes = payload.get("routes", [])
    if payload.get("source") not in ("pool", "live") or not 1 <= len(routes) <= 3:
        raise ValueError("response must name its source and return 1–3 routes")
    in_band = 0
    for route in routes:
        feature = route["feature"]
        coordinates = feature["geometry"]["coordinates"]
        if feature["geometry"]["type"] != "LineString" or len(coordinates) < 4:
            raise ValueError("route has no usable line geometry")
        if coordinates[0] != coordinates[-1]:
            raise ValueError("route does not finish where it starts")
        if not all(len(point) == 2 and all(math.isfinite(v) for v in point) for point in coordinates):
            raise ValueError("invalid route coordinates")
        metrics = feature["properties"]
        miles = metrics["distance_miles"]
        if not math.isfinite(miles) or miles <= 0:
            raise ValueError("invalid route distance")
        for name in ("traffic_signal_events", "major_crossing_events", "elevation_gain_feet"):
            if metrics[name] < 0:
                raise ValueError(f"negative {name}")
        for name in ("trail_fraction", "repeated_fraction", "elevation_coverage", "compactness"):
            if not 0 <= metrics[name] <= 1:
                raise ValueError(f"invalid {name}")
        track = ET.fromstring(route["gpx"])
        points = track.findall(".//{*}trkpt")
        if len(points) != len(coordinates):
            raise ValueError("GPX track and displayed route have different point counts")
        for point, (longitude, latitude) in zip(points, coordinates):
            if abs(float(point.attrib["lat"]) - latitude) > 0.000001 or abs(float(point.attrib["lon"]) - longitude) > 0.000001:
                raise ValueError("GPX track differs from the displayed route")
        in_band += abs(miles - target) <= 0.25001
    return {"source": payload["source"], "routes": len(routes), "in_band": in_band,
            "distances": [round(r["feature"]["properties"]["distance_miles"], 3) for r in routes],
            "warnings": payload.get("warnings", [])}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", required=True)
    parser.add_argument("--output", type=Path, default=ROOT / "output/presentation/live-check.json")
    parser.add_argument("--require-pool", action="store_true")
    parser.add_argument("--capture", action="store_true", help="save full responses for a local offline demo")
    parser.add_argument("--timeout", type=float, default=90)
    parser.add_argument("--expected-commit", help="require /api/version to identify this exact backend release")
    args = parser.parse_args(argv)
    api = args.api.rstrip("/")
    session = requests.Session()
    report = {"api": api, "checked_at": dt.datetime.now(dt.timezone.utc).isoformat(), "checks": [], "cases": []}
    args.output.parent.mkdir(parents=True, exist_ok=True)

    def get(path):
        response = session.get(api + path, timeout=args.timeout)
        response.raise_for_status()
        return response

    try:
        if get("/api/health").json().get("status") != "ok":
            raise ValueError("health check is not ok")
        report["checks"].append("health")
        if args.expected_commit:
            report["release"] = get("/api/version").json()
            if report["release"].get("commit") != args.expected_commit:
                raise ValueError("API is not running the expected release")
            report["checks"].append("backend-release")
        report["areas"] = get("/api/areas").json()["areas"]
        if not report["areas"]:
            raise ValueError("no supported areas")
        report["checks"].append("coverage")
        places = get("/api/places").json()["places"]
        if not places:
            raise ValueError("autocomplete has no places")
        report["place_count"] = len(places)
        report["checks"].append("autocomplete")
        page = get("/").text
        default = DefaultStart()
        default.feed(page)
        if set(default.values) != {"latitude", "longitude"}:
            raise ValueError("frontend has no valid default start")
        report["checks"].append("frontend")
        for asset in default.assets:
            if not get(asset).content:
                raise ValueError(f"empty frontend asset: {asset}")
        report["asset_count"] = len(default.assets)
        report["checks"].append("frontend-assets")
        manifest = json.loads((ROOT / "graphs/manifest.json").read_text())
        area = next(a for a in manifest["areas"] if a["slug"] == "charlottesville-va")
        center = {key: area[key] for key in ("latitude", "longitude")}
        cases = [(f"default-{miles}", default.values, {"distance_miles": miles}) for miles in (3, 5, 8, 12)]
        cases += [(f"center-5-{surface}", center, {"distance_miles": 5, "surface": surface})
                  for surface in ("mixed", "road", "trail")]
        cases += [(f"center-5-{elevation}", center, {"distance_miles": 5, "elevation": elevation})
                  for elevation in ("flat", "hilly")]
        cases += [("default-3-drive-1", default.values, {"distance_miles": 3, "drive_radius_miles": 1})]
        for name, origin, preferences in cases:
            body = {"surface": "mixed", "elevation": "balanced", "result_count": 3, **origin, **preferences}
            started = time.perf_counter()
            response = session.post(api + "/api/routes", json=body, timeout=args.timeout)
            response.raise_for_status()
            payload = response.json()
            result = {"name": name, **check_routes(payload, body["distance_miles"]),
                      "milliseconds": round((time.perf_counter() - started) * 1000)}
            report["cases"].append(result)
            if args.capture:
                directory = args.output.parent / "responses"
                directory.mkdir(exist_ok=True)
                (directory / f"{name}.json").write_text(json.dumps(payload) + "\n")
            print(f"{name}: {result['source']}, {result['routes']} closed routes, "
                  f"{result['in_band']} in band, GPX matches, {result['milliseconds']} ms", flush=True)
        for name, body in [("unsupported-start", {"latitude": 0, "longitude": 0, "distance_miles": 3}),
                           ("invalid-distance", {**default.values, "distance_miles": 0})]:
            response = session.post(api + "/api/routes", json=body, timeout=args.timeout)
            if response.status_code != 422 or not response.json().get("detail"):
                raise ValueError(f"{name} did not return an explained 422")
            report["checks"].append(name)
        if args.require_pool and not any(c["source"] == "pool" for c in report["cases"]):
            raise ValueError("no request was answered from a pool")
        report["passed"] = True
    except (requests.RequestException, ValueError, KeyError, TypeError, ET.ParseError) as error:
        report["passed"] = False
        report["error"] = str(error)
        print(f"FAILED: {error}", file=sys.stderr)
    finally:
        args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(f"Report: {args.output}", flush=True)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
