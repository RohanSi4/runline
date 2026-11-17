"""Validate the deployed RunLine app against the proposal's measurable criteria.

Covers US10 (response time), US11 (additional areas), US9 (drive radius),
US4 (displayed values, via scripts/ui_display_check.mjs), supporting US8
evidence, metadata and error handling. Starts are named public landmarks:
Charlottesville starts come from the deployed /api/places catalog and metro
starts are typed addresses the API geocodes, exactly as the page does. No
coordinates are stored in this file; they exist only in the ignored output.

    PYTHONPATH=$PWD/src .venv/bin/python scripts/validate_deployed.py
    PYTHONPATH=$PWD/src .venv/bin/python scripts/validate_deployed.py --phases meta,cville
    PYTHONPATH=$PWD/src .venv/bin/python scripts/validate_deployed.py --summarize

Requests are strictly sequential with a pause between them, counted across
invocations in requests.jsonl, and the run stops on a burst of 5xx answers.
Everything is written to the git-ignored output/deployed-validation/.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import statistics
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
PYTHON = sys.executable
DEFAULT_BASE = "https://runline-nine.vercel.app"
CAPSTONE_BASE = "https://runline-capstone.vercel.app"
TOLERANCE_MILES = 0.25
PHASES = ("meta", "cville", "metros", "errors", "us8", "drive", "p2p", "presentation", "direct", "ui")

# Public landmarks in the deployed Charlottesville place catalog (/api/places).
CVILLE_STARTS = [
    "The Lawn", "McIntire Park", "Pen Park", "Washington Park", "Court Square Park",
    "IX Art Park", "Riverview Park", "Greenleaf Park", "Forest Hills Park", "Rives Park",
]
# (start, drive radius, distance): ten drive-to-start cases.
DRIVE_CASES = [
    ("Jordan Park", 1, 3), ("Meade Park", 3, 3), ("Quarry Park", 5, 3),
    ("Jenkins Park", 1, 5), ("Azalea Park", 3, 5), ("Fifeville Park", 5, 5),
    ("Starr Hill Park", 1, 3), ("Longwood Park", 3, 8), ("Greenbrier Park", 5, 3),
    ("Northeast Park", 1, 5),
]
# Typed public-landmark addresses; the API geocodes them server-side. The last
# entry of each list is a spare, used only if an earlier one cannot be placed.
METRO_STARTS = {
    "Washington, DC": ["Washington Monument, Washington, DC", "Dupont Circle, Washington, DC",
                       "Lincoln Park, Washington, DC", "Meridian Hill Park, Washington, DC"],
    "New York": ["New York City Hall, New York, NY", "Bryant Park, New York, NY",
                 "Prospect Park, Brooklyn, NY", "Tompkins Square Park, New York, NY"],
    "Los Angeles": ["Los Angeles City Hall, Los Angeles, CA", "MacArthur Park, Los Angeles, CA",
                    "Echo Park Lake, Los Angeles, CA", "Pershing Square, Los Angeles, CA"],
    "Atlanta": ["Piedmont Park, Atlanta, GA", "Atlanta City Hall, Atlanta, GA",
                "Grant Park, Atlanta, GA", "Woodruff Park, Atlanta, GA"],
    "Boston": ["Boston Common, Boston, MA", "Boston City Hall, Boston, MA",
               "Cambridge City Hall, Cambridge, MA", "Christopher Columbus Park, Boston, MA"],
    "Phoenix": ["Phoenix City Hall, Phoenix, AZ", "Encanto Park, Phoenix, AZ",
                "Tempe Beach Park, Tempe, AZ", "Margaret T. Hance Park, Phoenix, AZ"],
}
METRO_STARTS_PER_AREA = 3
# Charlottesville places typed as addresses, compared against a map click at the geocoded point.
US8_ADDRESSES = ["The Rotunda, Charlottesville, VA", "Charlottesville City Hall, Charlottesville, VA"]


class Stop(Exception):
    """Request budget exhausted or a 5xx burst: stop sending requests."""


class Client:
    def __init__(self, base: str, out: Path, pause: float, budget: int):
        self.base = base.rstrip("/")
        self.out = out
        self.pause = pause
        self.log = out / "requests.jsonl"
        self.sent = sum(1 for _ in self.log.open()) if self.log.exists() else 0
        self.budget = budget
        self.consecutive_5xx = 0
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "runline-deployed-validation/1.0 (course QA script)"

    def call(self, method: str, path: str, *, phase: str, case: str, body: dict | None = None,
             base: str | None = None, timeout: float = 150) -> tuple[requests.Response | None, float]:
        if self.sent >= self.budget:
            raise Stop(f"request budget of {self.budget} reached")
        if self.consecutive_5xx >= 3:
            raise Stop("three consecutive 5xx answers; stopping")
        time.sleep(self.pause)
        url = (base or self.base) + path
        started = time.perf_counter()
        response, error = None, None
        try:
            response = self.session.request(method, url, json=body, timeout=timeout)
        except requests.RequestException as exc:
            error = f"{type(exc).__name__}: {exc}"
        ms = (time.perf_counter() - started) * 1000
        self.sent += 1
        status = response.status_code if response is not None else None
        self.consecutive_5xx = self.consecutive_5xx + 1 if (status is None or status >= 500) else 0
        with self.log.open("a") as handle:
            handle.write(json.dumps({"at": now(), "phase": phase, "case": case, "method": method,
                                     "url": url, "body": body, "status": status,
                                     "ms": round(ms, 1), "error": error}) + "\n")
        print(f"[{self.sent:3d}] {phase}/{case}: {status or error} in {ms:.0f} ms", flush=True)
        return response, ms


def parses_as_json(response: requests.Response) -> bool:
    try:
        response.json()
        return True
    except ValueError:
        return False


def decoded_text(response: requests.Response) -> str:
    """The body, decoding Brotli with Node's zlib when requests could not.

    The edge cache has served /api/places Brotli-encoded to clients that did
    not advertise br; requests has no Brotli decoder without an extra package.
    """
    if parses_as_json(response) or response.headers.get("content-encoding") != "br":
        return response.text
    script = "process.stdout.write(require('zlib').brotliDecompressSync(require('fs').readFileSync(0)))"
    return subprocess.run(["node", "-e", script], input=response.content, capture_output=True, check=True).stdout.decode()


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def haversine_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Metres between two (lat, lon) points."""
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * 6371008.8 * math.asin(math.sqrt(h))


def save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=1) + "\n")


def load_json(path: Path, default=None):
    return json.loads(path.read_text()) if path.exists() else default


# --- route analysis --------------------------------------------------------

def surface_penalty(surface: str, trail: float) -> float:
    if surface == "road":
        return trail
    if surface == "trail":
        return 1 - trail
    return abs(trail - 0.5) * 0.6


def expected_components(p: dict, target: float, surface: str) -> dict:
    """Recompute src/runline/scoring.py's components from the returned metrics.

    Elevation is normalized over every candidate the planner saw, which the
    response does not include, so it is only range-checked by the caller.
    """
    d = p["distance_miles"]
    error = abs(d - target)
    distance = 0.15 * error / TOLERANCE_MILES if error <= TOLERANCE_MILES else error / TOLERANCE_MILES
    return {
        "distance": 7.0 * distance,
        "traffic_signals": 6.0 * p["traffic_signal_events"] / max(d, 1),
        "major_crossings": 4.0 * p["major_crossing_events"] / max(d, 1),
        "surface": 2.5 * surface_penalty(surface, p["trail_fraction"]),
        "repeated_segments": 5.0 * p["repeated_fraction"],
        "shape": 4.0 * max(0.0, 0.35 - p["compactness"]) / 0.35,
        "driving": 0.35 * p.get("drive_distance_miles", 0),
    }


def analyze_route(route: dict, target: float | None, surface: str, origin: tuple | None, closed: bool = True) -> dict:
    feature = route.get("feature") or {}
    geometry = feature.get("geometry") or {}
    coords = geometry.get("coordinates") or []
    p = feature.get("properties") or {}
    d = p.get("distance_miles")
    result = {"distance_miles": d, "source_ok": True, "problems": []}
    problems = result["problems"]
    if geometry.get("type") != "LineString" or len(coords) < (4 if closed else 2):
        problems.append("no usable LineString")
    if closed:
        result["closed"] = bool(coords) and coords[0] == coords[-1]
        if not result["closed"]:
            problems.append("first point != last point")
    try:
        points = ET.fromstring(route.get("gpx") or "").findall(".//{*}trkpt")
        result["gpx_parses"] = True
        result["gpx_points"] = len(points)
        result["gpx_matches"] = len(points) == len(coords) and all(
            abs(float(pt.attrib["lat"]) - lat) <= 1e-6 and abs(float(pt.attrib["lon"]) - lon) <= 1e-6
            for pt, (lon, lat) in zip(points, coords))
    except ET.ParseError as error:
        result.update(gpx_parses=False, gpx_matches=False, gpx_error=str(error))
    if not result.get("gpx_parses"):
        problems.append("GPX missing or does not parse")
    elif not result["gpx_matches"]:
        problems.append("GPX track differs from returned geometry")
    track_m = sum(haversine_m((a[1], a[0]), (b[1], b[0])) for a, b in zip(coords, coords[1:]))
    result["geometry_miles"] = track_m / 1609.344
    if isinstance(d, (int, float)) and d > 0:
        result["geometry_vs_reported_pct"] = 100 * (result["geometry_miles"] - d) / d
    if origin and coords:
        result["first_point_offset_m"] = round(haversine_m(origin, (coords[0][1], coords[0][0])), 1)
    if target is not None and isinstance(d, (int, float)):
        error = d - target
        result.update(error_miles=error, in_band=abs(error) <= TOLERANCE_MILES + 1e-9,
                      within_5pct=abs(error) <= 0.05 * target + 1e-9,
                      within_10pct=abs(error) <= 0.10 * target + 1e-9)
        components = p.get("score_components") or {}
        expected = expected_components(p, target, surface)
        mismatches = {k: [components.get(k), v] for k, v in expected.items()
                      if components.get(k) is None or abs(components[k] - v) > 1e-6 * max(1, abs(v))}
        elevation = components.get("elevation")
        if elevation is None or not (-1e-9 <= elevation <= 1.5 + 1e-9):
            mismatches["elevation"] = [elevation, "0..1.5"]
        if p.get("score") is None or abs(sum(components.values()) - p["score"]) > 1e-6 * max(1, abs(p["score"])):
            mismatches["score_sum"] = [p.get("score"), sum(components.values())]
        result["score_mismatches"] = mismatches
        result["score"] = p.get("score")
        result["awkward"] = p.get("compactness", 1) < 0.2 or p.get("repeated_fraction", 0) > 0.10
    start = p.get("start") or {}
    result["start"] = {"label": start.get("label"), "kind": start.get("kind"),
                       "drive_distance_miles": start.get("drive_distance_miles")}
    if origin and start.get("coordinate"):
        c = start["coordinate"]
        result["start"]["straight_line_miles"] = haversine_m(origin, (c["latitude"], c["longitude"])) / 1609.344
    for key in ("traffic_signal_events", "major_crossing_events", "elevation_gain_feet",
                "trail_fraction", "repeated_fraction", "compactness", "elevation_coverage"):
        result[key] = p.get(key)
    return result


def ranking_consistent(routes: list[dict], target: float) -> bool:
    """Returned order follows scoring.rank_candidates' key."""
    def key(r):
        if r["in_band"]:
            return (0, r["awkward"], r["score"], abs(r["error_miles"]))
        return (1, abs(r["error_miles"]), r["score"])
    keys = [key(r) for r in routes]
    return keys == sorted(keys)


def analyze_loop(payload: dict, target: float, surface: str, origin: tuple | None) -> dict:
    routes = payload.get("routes") or []
    analyzed = [analyze_route(r, target, surface, origin) for r in routes]
    problems = []
    if payload.get("source") not in ("pool", "live"):
        problems.append(f"source {payload.get('source')!r}")
    if not 1 <= len(routes) <= 3:
        problems.append(f"{len(routes)} routes")
    for index, route in enumerate(analyzed, 1):
        problems += [f"route {index}: {p}" for p in route["problems"]]
    top_in_band = bool(analyzed) and analyzed[0]["in_band"]
    if analyzed and not top_in_band:
        problems.append("top route outside +/-0.25 mi")
    return {"source": payload.get("source"), "route_count": len(routes), "routes": analyzed,
            "warnings": payload.get("warnings", []), "discovered_starts": payload.get("discovered_starts"),
            "top_in_band": top_in_band, "all_in_band": all(r["in_band"] for r in analyzed),
            "ranking_consistent": ranking_consistent(analyzed, target) if analyzed else None,
            "score_mismatch_count": sum(bool(r["score_mismatches"]) for r in analyzed),
            "problems": problems, "passed": not problems}


# --- phases ------------------------------------------------------------------

def loop_body(distance: float, *, lat=None, lon=None, address=None, radius=0, surface="mixed") -> dict:
    """The body web/app.js sends for a loop request."""
    body = {"mode": "loop", "distance_miles": distance, "drive_radius_miles": radius,
            "surface": surface, "elevation": "balanced", "result_count": 3}
    if address:
        body["address"] = address
    else:
        body.update(latitude=lat, longitude=lon)
    return body


def route_case(client: Client, out: Path, phase: str, case: str, body: dict, **meta) -> dict:
    response, ms = client.call("POST", "/api/routes", phase=phase, case=case, body=body)
    record = {"case": case, "request": body, "ms": round(ms, 1), "status": getattr(response, "status_code", None), **meta}
    payload = None
    if response is not None:
        try:
            payload = response.json()
        except ValueError:
            record["body_text"] = response.text[:500]
    if record["status"] == 200 and isinstance(payload, dict):
        origin = payload.get("origin") or {}
        o = (origin.get("latitude"), origin.get("longitude")) if origin else None
        record["origin"] = origin
        record["analysis"] = analyze_loop(payload, body["distance_miles"], body["surface"], o)
        save_json(out / "captures" / phase / f"{case}.json", {"request": body, "status": 200, "ms": ms, "meta": meta, "response": payload})
    else:
        record["detail"] = payload.get("detail") if isinstance(payload, dict) else None
    return record


def find_place(places: list[dict], name: str) -> dict | None:
    named = [p for p in places if p["name"] == name]
    return next((p for p in named if p.get("kind") != "street"), named[0] if named else None)


def phase_meta(client: Client, out: Path) -> dict:
    meta = {"checked_at": now(), "base": client.base}
    for path in ("/api/health", "/api/version", "/api/areas", "/api/capabilities"):
        response, ms = client.call("GET", path, phase="meta", case=path)
        meta[path] = {"status": response.status_code, "ms": round(ms), "body": response.json()} if response is not None else None
    response, _ = client.call("GET", "/api/places", phase="meta", case="/api/places")
    places = json.loads(decoded_text(response))["places"]
    save_json(out / "places.json", places)
    meta["/api/places"] = {"status": response.status_code, "count": len(places),
                           "cache_control": response.headers.get("cache-control"),
                           "content_encoding": response.headers.get("content-encoding"),
                           "x_vercel_cache": response.headers.get("x-vercel-cache"),
                           "client_accept_encoding": client.session.headers.get("Accept-Encoding"),
                           "parses_without_brotli": parses_as_json(response)}
    for path, name in (("/", "index.html"), ("/assets/app.js", "deployed-app.js")):
        response, _ = client.call("GET", path, phase="meta", case=path)
        (out / name).write_text(decoded_text(response))
        local = (ROOT / "web" / ("index.html" if name == "index.html" else "app.js")).read_text()
        meta[path] = {"status": response.status_code, "bytes": len(decoded_text(response)), "matches_worktree": decoded_text(response) == local}
    # The rewrite target for /api/warm is the regional backend; ask it which commit it runs.
    remote = next(r["dest"] for r in json.loads((ROOT / "vercel.json").read_text())["routes"] if r["src"] == "/api/warm")
    remote_base = remote.rsplit("/api/warm", 1)[0]
    response, _ = client.call("GET", "/api/version", phase="meta", case="regional /api/version", base=remote_base)
    meta["regional /api/version"] = response.json() if response is not None and response.ok else None
    save_json(out / "results" / "meta.json", meta)
    return meta


def phase_cville(client: Client, out: Path) -> list:
    places = load_json(out / "places.json")
    records = []
    for name in CVILLE_STARTS:
        place = find_place(places, name)
        if place is None:
            records.append({"case": name, "status": None, "detail": "not in deployed /api/places"})
            continue
        # Choosing a suggestion calls /api/warm (after a 400 ms debounce); then the runner submits.
        response, ms = client.call("POST", "/api/warm", phase="cville", case=f"{name} warm",
                                   body={"latitude": place["latitude"], "longitude": place["longitude"]})
        warm = {"status": getattr(response, "status_code", None), "ms": round(ms),
                "body": response.json() if response is not None and response.ok else None}
        time.sleep(1.5)
        for rep, distance in ((1, 3), (1, 5), (2, 3), (2, 5)):
            case = f"{name} {distance}mi rep{rep}".replace(" ", "_")
            record = route_case(client, out, "cville", case,
                                loop_body(distance, lat=place["latitude"], lon=place["longitude"]),
                                area="Charlottesville, VA", start=name, distance=distance, rep=rep,
                                cold=(rep == 1 and distance == 3), warm_call=warm)
            records.append(record)
    save_json(out / "results" / "cville.json", records)
    return records


def phase_metros(client: Client, out: Path) -> list:
    records = []
    for area, addresses in METRO_STARTS.items():
        used = 0
        for address in addresses:
            if used == METRO_STARTS_PER_AREA:
                break
            label = address.split(",")[0]
            slug = f"{area} {label}".replace(" ", "_").replace(",", "")
            # Typed address (no suggestions outside Charlottesville): the first request geocodes.
            first = route_case(client, out, "metros", f"{slug}_5mi_address", loop_body(5, address=address),
                               area=area, start=label, distance=5, cold=True, by="address")
            records.append(first)
            if first["status"] != 200:
                continue
            used += 1
            origin = first["origin"]
            # renderRoutes() moves the pin to the origin, which calls /api/warm.
            response, ms = client.call("POST", "/api/warm", phase="metros", case=f"{slug} warm",
                                       body={"latitude": origin["latitude"], "longitude": origin["longitude"]})
            warm = {"status": getattr(response, "status_code", None), "ms": round(ms),
                    "body": response.json() if response is not None and response.ok else None}
            time.sleep(1.5)
            for distance in (3, 8, 5):
                records.append(route_case(client, out, "metros", f"{slug}_{distance}mi",
                                          loop_body(distance, lat=origin["latitude"], lon=origin["longitude"]),
                                          area=area, start=label, distance=distance, cold=False, by="coordinates",
                                          warm_call=warm))
    save_json(out / "results" / "metros.json", records)
    return records


def phase_drive(client: Client, out: Path) -> list:
    places = load_json(out / "places.json")
    records = []
    for name, radius, distance in DRIVE_CASES:
        place = find_place(places, name)
        if place is None:
            records.append({"case": name, "status": None, "detail": "not in deployed /api/places"})
            continue
        case = f"{name}_r{radius}_{distance}mi".replace(" ", "_")
        record = route_case(client, out, "drive", case,
                            loop_body(distance, lat=place["latitude"], lon=place["longitude"], radius=radius),
                            area="Charlottesville, VA", start=name, distance=distance, radius=radius)
        if record.get("analysis"):
            alternatives = []
            for index, route in enumerate(record["analysis"]["routes"], 1):
                start = route["start"]
                if start.get("kind") in (None, "origin"):
                    continue
                drive = start.get("drive_distance_miles")
                straight = start.get("straight_line_miles")
                alternatives.append({"route": index, "label": start.get("label"), "kind": start.get("kind"),
                                     "drive_miles": drive, "straight_line_miles": straight,
                                     "within_radius_drive": drive is not None and drive <= radius + 1e-9,
                                     "within_radius_straight": straight is not None and straight <= radius + 1e-9})
            record["alternatives"] = alternatives
        records.append(record)
    save_json(out / "results" / "drive.json", records)
    return records


def phase_errors(client: Client, out: Path) -> list:
    places = load_json(out / "places.json")
    lawn = find_place(places, "The Lawn")
    at_lawn = {"latitude": lawn["latitude"], "longitude": lawn["longitude"]}
    cases = [
        ("unsupported start (0,0)", {"latitude": 0, "longitude": 0, "distance_miles": 3}),
        ("unsupported city by address", {"address": "Millennium Park, Chicago, IL", "distance_miles": 3}),
        ("unknown address", {"address": "Qzxv Nonexistent Placename 99999", "distance_miles": 3}),
        ("distance 0", {**at_lawn, "distance_miles": 0}),
        ("distance 31", {**at_lawn, "distance_miles": 31}),
        ("missing start", {"distance_miles": 3}),
        ("missing distance", {**at_lawn}),
        ("drive radius 2", {**at_lawn, "distance_miles": 3, "drive_radius_miles": 2}),
    ]
    records = []
    for name, body in cases:
        response, ms = client.call("POST", "/api/routes", phase="errors", case=name, body=body)
        status = getattr(response, "status_code", None)
        try:
            detail = response.json().get("detail")
        except (ValueError, AttributeError):
            detail = None
        explained = bool(detail) and (isinstance(detail, str) or all(isinstance(d, dict) and d.get("msg") for d in detail))
        records.append({"case": name, "request": body, "status": status, "ms": round(ms), "detail": detail,
                        "passed": status is not None and 400 <= status < 500 and explained})
    save_json(out / "results" / "errors.json", records)
    return records


def phase_us8(client: Client, out: Path) -> list:
    """A typed address, then a map click at the point it resolved to, give the same routes."""
    records = []
    for address in US8_ADDRESSES:
        label = address.split(",")[0].replace(" ", "_")
        typed = route_case(client, out, "us8", f"{label}_typed", loop_body(5, address=address), by="address")
        record = {"address": address, "typed": typed}
        if typed["status"] == 200:
            o = typed["origin"]
            # A map click sends toFixed(6) coordinates (web/app.js map.on("click")).
            lat, lon = round(o["latitude"], 6), round(o["longitude"], 6)
            clicked = route_case(client, out, "us8", f"{label}_mapclick", loop_body(5, lat=lat, lon=lon), by="map click")
            record["clicked"] = clicked
            if clicked["status"] == 200:
                a = load_json(out / "captures" / "us8" / f"{label}_typed.json")["response"]["routes"]
                b = load_json(out / "captures" / "us8" / f"{label}_mapclick.json")["response"]["routes"]
                record["same_route_count"] = len(a) == len(b)
                record["same_geometry"] = [x["feature"]["geometry"] == y["feature"]["geometry"] for x, y in zip(a, b)]
                record["same_distances"] = [x["feature"]["properties"]["distance_miles"] == y["feature"]["properties"]["distance_miles"] for x, y in zip(a, b)]
        records.append(record)
    save_json(out / "results" / "us8.json", records)
    return records


def phase_p2p(client: Client, out: Path) -> dict:
    """One Start -> finish request between two catalog landmarks, checked like check_direct_routes.py."""
    places = load_json(out / "places.json")
    a, b = find_place(places, "The Lawn"), find_place(places, "McIntire Park")
    body = {"mode": "point_to_point", "latitude": a["latitude"], "longitude": a["longitude"],
            "destination": {"latitude": b["latitude"], "longitude": b["longitude"]},
            "drive_radius_miles": 0, "surface": "mixed", "elevation": "balanced", "result_count": 3}
    response, ms = client.call("POST", "/api/routes", phase="p2p", case="The Lawn to McIntire Park", body=body)
    record = {"case": "The Lawn to McIntire Park", "ms": round(ms), "status": getattr(response, "status_code", None), "problems": []}
    if record["status"] == 200:
        payload = response.json()
        save_json(out / "captures" / "p2p" / "lawn-to-mcintire.json", {"request": body, "status": 200, "ms": ms, "response": payload})
        record.update(mode=payload.get("mode"), source=payload.get("source"), routes=[])
        if payload.get("mode") != "point_to_point":
            record["problems"].append("mode is not point_to_point")
        for route in payload.get("routes", []):
            r = analyze_route(route, None, "mixed", None, closed=False)
            pts = route["feature"]["geometry"]["coordinates"]
            ends = (pts[0] == [payload["origin"]["longitude"], payload["origin"]["latitude"]]
                    and pts[-1] == [payload["destination"]["longitude"], payload["destination"]["latitude"]] and pts[0] != pts[-1])
            if not ends:
                r["problems"].append("path does not run from origin to destination")
            record["routes"].append({"distance_miles": r["distance_miles"], "gpx_matches": r.get("gpx_matches"),
                                     "endpoints_ok": ends, "problems": r["problems"]})
            record["problems"] += r["problems"]
        record["passed"] = not record["problems"] and bool(record["routes"])
    else:
        record["passed"] = False
        record["detail"] = response.text[:300] if response is not None else None
    save_json(out / "results" / "p2p.json", record)
    return record


def run_subprocess(name: str, command: list[str], out: Path, client: Client, estimate: int) -> dict:
    if client.sent + estimate > client.budget:
        raise Stop(f"{name} needs about {estimate} requests; budget left {client.budget - client.sent}")
    started = time.perf_counter()
    result = subprocess.run(command, capture_output=True, text=True, cwd=ROOT)
    client.sent += estimate
    with client.log.open("a") as handle:
        handle.write(json.dumps({"at": now(), "phase": name, "case": "subprocess (estimated count)",
                                 "estimated_requests": estimate, "returncode": result.returncode}) + "\n")
    record = {"command": [c if not c.startswith(str(ROOT)) else str(Path(c).relative_to(ROOT)) for c in command],
              "returncode": result.returncode, "seconds": round(time.perf_counter() - started, 1),
              "stdout": result.stdout[-6000:], "stderr": result.stderr[-3000:]}
    save_json(out / "results" / f"{name}.json", record)
    print(result.stdout, result.stderr, sep="\n", flush=True)
    return record


def phase_presentation(client: Client, out: Path) -> dict:
    """The team's rehearsal, against production and against the backend URL it rewrites to."""
    commit = (load_json(out / "results" / "meta.json", {}).get("/api/version") or {}).get("body", {}).get("commit")
    records = {}
    for label, base in (("nine", client.base), ("capstone", CAPSTONE_BASE)):
        command = [PYTHON, str(ROOT / "scripts/presentation_check.py"), "--api", base, "--require-pool",
                   "--capture", "--output", str(out / "presentation" / label / "presentation-check.json")]
        if commit and commit != "unknown":
            command += ["--expected-commit", commit]
        # health, version, areas, places, page, assets (about 6), ten routes, two errors
        records[label] = run_subprocess(f"presentation-{label}", command, out, client, estimate=22)
    return records


def phase_direct(client: Client, out: Path) -> dict:
    command = [PYTHON, str(ROOT / "scripts/check_direct_routes.py"), "--api", client.base,
               "--output", str(out / "direct-check.json")]
    return run_subprocess("direct", command, out, client, estimate=8)


def phase_ui(out: Path) -> dict:
    command = ["node", str(ROOT / "scripts/ui_display_check.mjs"), str(out)]
    result = subprocess.run(command, capture_output=True, text=True, cwd=ROOT)
    print(result.stdout[-4000:], result.stderr[-4000:], sep="\n", flush=True)
    tests = subprocess.run(["node", "--test", "tests/web_flow.mjs", "tests/run_session.mjs"], capture_output=True, text=True, cwd=ROOT)
    tail = [line for line in tests.stdout.splitlines() if line.startswith(("# tests", "# pass", "# fail"))]
    save_json(out / "node-tests.json", {"returncode": tests.returncode, "summary": "; ".join(tail)})
    return {"returncode": result.returncode, "node_tests": tests.returncode}


# --- summary -----------------------------------------------------------------

def p95(values: list[float]) -> float | None:
    """Nearest-rank 95th percentile."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)]


def timing(values_ms: list[float]) -> dict:
    s = [v / 1000 for v in values_ms]
    return {"n": len(s), "median_s": statistics.median(s) if s else None, "p95_s": p95(s), "max_s": max(s) if s else None,
            "min_s": min(s) if s else None}


def fmt(value, digits=2) -> str:
    return "n/a" if value is None else f"{value:.{digits}f}"


def ok_routes(records):
    return [r for r in records if r.get("status") == 200 and r.get("analysis")]


def summarize(out: Path) -> str:
    meta = load_json(out / "results" / "meta.json", {})
    cville = load_json(out / "results" / "cville.json", [])
    metros = load_json(out / "results" / "metros.json", [])
    drive = load_json(out / "results" / "drive.json", [])
    errors = load_json(out / "results" / "errors.json", [])
    us8 = load_json(out / "results" / "us8.json", [])
    presentation = {label: load_json(out / "results" / f"presentation-{label}.json") for label in ("nine", "capstone")}
    presentation = {k: v for k, v in presentation.items() if v}
    p2p = load_json(out / "results" / "p2p.json")
    direct = load_json(out / "results" / "direct.json")
    ui = load_json(out / "ui-check.json")
    log = [json.loads(line) for line in (out / "requests.jsonl").open()] if (out / "requests.jsonl").exists() else []
    sent = sum(entry.get("estimated_requests", 1) for entry in log)
    statuses = [e.get("status") for e in log if "status" in e]
    lines = [f"# Deployed validation summary", "",
             f"Generated {now()} from raw results in this directory.", "",
             f"- Base URL: {meta.get('base')}",
             f"- Backend `/api/version`: `{(meta.get('/api/version') or {}).get('body')}`",
             f"- Regional (Oracle) `/api/version`: `{meta.get('regional /api/version')}`",
             f"- `/api/areas`: {(meta.get('/api/areas') or {}).get('body', {}).get('areas')}",
             f"- `/api/capabilities`: `{(meta.get('/api/capabilities') or {}).get('body')}`",
             f"- `/api/health`: `{(meta.get('/api/health') or {}).get('body')}`",
             f"- Deployed `/assets/app.js` identical to worktree `web/app.js`: {(meta.get('/assets/app.js') or {}).get('matches_worktree')}",
             f"- Deployed `/` identical to worktree `web/index.html`: {(meta.get('/') or {}).get('matches_worktree')}",
             f"- `/api/places` encoding: {(meta.get('/api/places') or {}).get('content_encoding')} (x-vercel-cache {(meta.get('/api/places') or {}).get('x_vercel_cache')}) "
             f"to a client advertising `{(meta.get('/api/places') or {}).get('client_accept_encoding')}`; parses without a Brotli decoder: {(meta.get('/api/places') or {}).get('parses_without_brotli')}",
             f"- Requests sent (all invocations, subprocesses estimated): {sent}; "
             f"5xx answers: {sum(1 for s in statuses if s is not None and s >= 500)}; transport errors: {sum(1 for s in statuses if s is None)}",
             ""]
    rows = []

    # US10
    def area_timing(records, warm_only=True, distances=(3, 5)):
        rs = [r for r in records if r.get("status") == 200 and r.get("distance") in distances and (not r.get("cold")) == warm_only]
        return rs
    lines += ["## US10 response time (end to end from this machine)", "",
              "| Area | Sample | n | median s | p95 s | max s | pool / live |", "|---|---|---:|---:|---:|---:|---|"]
    us10_ok = True
    areas = [("Charlottesville, VA", cville)] + [(a, [r for r in metros if r.get("area") == a]) for a in METRO_STARTS]
    all_warm = []
    for area, records in areas:
        for label, rs in (("warm 3+5 mi", area_timing(records)), ("cold first", [r for r in records if r.get("cold") and r.get("status") == 200]),
                          ("8 mi", [r for r in records if r.get("status") == 200 and r.get("distance") == 8])):
            if not rs:
                continue
            t = timing([r["ms"] for r in rs])
            sources = [r["analysis"]["source"] for r in rs]
            lines.append(f"| {area} | {label} | {t['n']} | {fmt(t['median_s'])} | {fmt(t['p95_s'])} | {fmt(t['max_s'])} | "
                         f"{sources.count('pool')} / {sources.count('live')} |")
            if label.startswith("warm"):
                all_warm += rs
                if t["median_s"] >= 10 or t["p95_s"] >= 20:
                    us10_ok = False
        failed = [r for r in records if r.get("status") != 200 and not r.get("cold")]
        if failed:
            us10_ok = False
    tw = timing([r["ms"] for r in all_warm])
    lines += ["", f"All areas, warm 3+5 mi: n={tw['n']}, median {fmt(tw['median_s'])} s, p95 {fmt(tw['p95_s'])} s, max {fmt(tw['max_s'])} s.",
              "Cold = first request per start (Charlottesville: first after the page's /api/warm; metros: the typed-address request, which also geocodes). p95 is nearest-rank.", ""]
    expected_warm = len(CVILLE_STARTS) * 3 + len(METRO_STARTS) * METRO_STARTS_PER_AREA * 2
    rows.append(("US10", "PASS" if us10_ok and tw["n"] == expected_warm else ("FAIL" if not us10_ok else "PARTIAL"),
                 f"warm 3/5 mi n={tw['n']} (planned {expected_warm}); all-area median {fmt(tw['median_s'])} s, p95 {fmt(tw['p95_s'])} s, max {fmt(tw['max_s'])} s; per-area table below"))

    # US11
    lines += ["## US11 additional areas (3/5/8 mi, public landmarks)", "",
              "| Area | starts placed | requests | 200 | closed | GPX parses + matches | top in band | all routes in band | routes within 5% / 10% | score recompute mismatches | pool / live |",
              "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|"]
    passing_areas = []
    for area, records in areas:
        rs = ok_routes(records)
        routes = [x for r in rs for x in r["analysis"]["routes"]]
        starts = len({r["start"] for r in rs})
        closed = sum(x.get("closed", False) for x in routes)
        gpx = sum(x.get("gpx_matches", False) for x in routes)
        top = sum(r["analysis"]["top_in_band"] for r in rs)
        inb = sum(x["in_band"] for x in routes)
        w5 = sum(x["within_5pct"] for x in routes)
        w10 = sum(x["within_10pct"] for x in routes)
        mism = sum(bool(x["score_mismatches"]) for x in routes)
        sources = [r["analysis"]["source"] for r in rs]
        lines.append(f"| {area} | {starts} | {len(records)} | {len(rs)} | {closed}/{len(routes)} | {gpx}/{len(routes)} | {top}/{len(rs)} | "
                     f"{inb}/{len(routes)} | {w5}/{len(routes)} / {w10}/{len(routes)} | {mism} | {sources.count('pool')} / {sources.count('live')} |")
        if area != "Charlottesville, VA" and rs and all(r["analysis"]["passed"] for r in rs) and len(rs) == len(records) and starts >= 3:
            passing_areas.append(area)
    lines += ["", "A request passes when it returns 1-3 routes naming pool or live, every route is closed with a parseable GPX whose track equals the geometry point for point (1e-6 deg), and the top route is within +/-0.25 mi.", ""]
    failing = [(r.get("area"), r["case"], r.get("status"), (r.get("analysis") or {}).get("problems") or r.get("detail"))
               for _, records in areas for r in records if not (r.get("analysis") or {}).get("passed")]
    if failing:
        lines += ["Failing or unplaced requests:", ""] + [f"- {a} `{c}`: status {s}; {p}" for a, c, s, p in failing] + [""]
    rows.append(("US11", "PASS" if passing_areas else "FAIL",
                 f"{len(passing_areas)}/{len(METRO_STARTS)} additional areas passed every request ({', '.join(passing_areas) or 'none'})"))

    # presentation check
    for label, record in presentation.items():
        report = load_json(out / "presentation" / label / "presentation-check.json", {})
        lines += [f"## scripts/presentation_check.py --require-pool against {report.get('api')}", "",
                  f"Return code {record['returncode']}; passed={report.get('passed')}; checks={report.get('checks')}; error={report.get('error')}", ""]
        if report.get("cases"):
            lines += ["| case | source | routes | in band | ms |", "|---|---|---:|---:|---:|"]
            lines += [f"| {c['name']} | {c['source']} | {c['routes']} | {c['in_band']} | {c['milliseconds']} |" for c in report["cases"]]
        lines.append("")

    # US9
    if drive:
        lines += ["## US9 drive radius (Charlottesville)", "",
                  "| case | status | s | discovered | routes from alt. starts | alt. drive mi (straight mi) | within radius |",
                  "|---|---:|---:|---:|---:|---|---|"]
        violations, alt_total, cases_with_alt = 0, 0, 0
        for r in drive:
            alts = r.get("alternatives") or []
            alt_total += len(alts)
            cases_with_alt += bool(alts)
            bad = [a for a in alts if not (a["within_radius_drive"] and a["within_radius_straight"])]
            violations += len(bad)
            desc = "; ".join(f"{fmt(a['drive_miles'])} ({fmt(a['straight_line_miles'])})" for a in alts) or "-"
            lines.append(f"| {r['case']} | {r.get('status')} | {fmt(r.get('ms', 0) / 1000, 1)} | {(r.get('analysis') or {}).get('discovered_starts')} | "
                         f"{len(alts)} | {desc} | {'yes' if not bad else 'NO'} |")
        ui_drive = (ui or {}).get("drive", {})
        lines += ["", f"Alternative-start routes: {alt_total} in {cases_with_alt}/{len(drive)} cases; radius violations: {violations}.",
                  f"UI shows drive cost: {ui_drive.get('drive_cost_displayed')}; UI shows route score: {ui_drive.get('score_displayed')}; "
                  f"UI shows alternative start label: {ui_drive.get('start_label_displayed')}.", ""]
        api_ok = violations == 0 and all(r.get("status") == 200 for r in drive) and alt_total > 0
        display_ok = bool(ui_drive.get("drive_cost_displayed")) and bool(ui_drive.get("score_displayed"))
        drive_times = timing([r["ms"] for r in drive if r.get("status") == 200])
        below = sum(1 for r in drive for a in (r.get("alternatives") or []) if a["drive_miles"] is not None and a["straight_line_miles"] is not None and a["drive_miles"] < a["straight_line_miles"])
        lines += [f"Drive-enabled request time (always live): median {fmt(drive_times['median_s'])} s, p95 {fmt(drive_times['p95_s'])} s, max {fmt(drive_times['max_s'])} s.",
                  f"Reported drive distance is below the straight-line distance for {below}/{alt_total} alternative starts (it is measured between the nearest drive-graph nodes, so snap legs are not counted).", ""]
        # Every clause must hold for PASS; a clause that is demonstrably false is a FAIL.
        rows.append(("US9", "PASS" if api_ok and display_ok else "FAIL",
                     f"(a) radius: {'PASS' if api_ok else 'FAIL'}, {len(drive)} cases, {alt_total} alt-start routes, {violations} outside the radius; "
                     f"(b) display of score + drive cost: {'PASS' if display_ok else 'FAIL'} (drive cost shown {ui_drive.get('drive_cost_displayed')}, score shown {ui_drive.get('score_displayed')})"))

    # US4
    if ui:
        u = ui.get("display", {})
        lines += ["## US4 displayed values (Node VM harness, real responses)", "",
                  f"Source loaded: {ui.get('source')} (identical to deployed: {ui.get('source_matches_deployed')})", "",
                  f"- Responses rendered: {u.get('responses')}; cards: {u.get('cards')}; displayed numbers compared: {u.get('compared')}; mismatches: {len(u.get('mismatches', []))}",
                  f"- Shown per card: {', '.join(u.get('shown', []))}",
                  f"- Not shown anywhere: {', '.join(u.get('not_shown', []))}",
                  f"- Scorer components (score_components): {', '.join(u.get('scorer_components', []))}", ""]
        for m in u.get("mismatches", [])[:20]:
            lines.append(f"  - mismatch: {m}")
        lines += ["| scorer component (scoring.rank_candidates) | displayed in the UI |", "|---|---|"]
        lines += [f"| {k} | {v} |" for k, v in u.get("component_display", {}).items()] + [""]
        values_ok = not u.get("mismatches") and u.get("cards", 0) > 0
        shows_error = "distance error / off-target" not in u.get("not_shown", [])
        components_ok = shows_error and u.get("score_parts_shown")
        rows.append(("US4", "PASS" if values_ok and components_ok else "FAIL",
                     f"(a) shown values match the JSON: {'PASS' if values_ok else 'FAIL'}, {u.get('compared')} checks over {u.get('cards')} cards, "
                     f"{len(u.get('mismatches', []))} mismatches; (b) distance error and major scoring components displayed: "
                     f"{'PASS' if components_ok else 'FAIL'} (not shown: {', '.join(u.get('not_shown', []))})"))

    # US8
    us8_ok = True
    if ui or us8:
        a = (ui or {}).get("autocomplete", [])
        mc = (ui or {}).get("map_click", {})
        lines += ["## US8 supporting evidence (not a substitute for the manual browser checklist)", ""]
        for item in a:
            lines.append(f"- autocomplete `{item['query']}` -> expected `{item['expected']}` listed: {item['listed']}; choosing it sets the pin to the catalog point: {item['pin_set']}; warm sent: {item['warm_sent']}; route body uses coordinates: {item['body_coordinates']}")
            us8_ok &= item["listed"] and item["pin_set"] and item["body_coordinates"]
        if mc:
            lines.append(f"- map click: body has toFixed(6) coordinates and no address: {mc.get('passed')}")
            us8_ok &= bool(mc.get("passed"))
        for r in us8:
            same = r.get("same_geometry")
            lines.append(f"- typed `{r['address']}`: status {r['typed'].get('status')}; map click at the resolved point: status {(r.get('clicked') or {}).get('status')}; identical routes: {same}")
            us8_ok &= bool(same) and all(same)
        lines.append("")
    node_tests = load_json(out / "node-tests.json")
    if node_tests:
        lines.append(f"- `node --test tests/web_flow.mjs tests/run_session.mjs`: {node_tests.get('summary')}")
        us8_ok &= node_tests.get("returncode") == 0
        lines.append("")
    rows.append(("US8 (supporting)", "PASS" if us8_ok else "FAIL", "automated evidence only; the three-person manual browser checklist is still required"))

    # Errors and point to point
    lines += ["## Error handling", "", "| case | status | explained detail |", "|---|---:|---|"]
    lines += [f"| {r['case']} | {r['status']} | {str(r['detail'])[:110]} |" for r in errors]
    lines.append("")
    rows.append(("Errors", "PASS" if errors and all(r["passed"] for r in errors) else "FAIL",
                 f"{sum(r['passed'] for r in errors)}/{len(errors)} returned an explained 4xx"))
    if direct:
        report = load_json(out / "direct-check.json", {})
        lines += ["## Point to point (scripts/check_direct_routes.py)", "",
                  f"Return code {direct['returncode']}; cases: {report.get('cases')}; stderr tail: `{direct['stderr'][-300:].strip()}`", ""]
        reason = report.get("cases") if direct["returncode"] == 0 else "stopped before any route request: " + (direct["stderr"].strip().splitlines() or ["?"])[-1]
        rows.append(("check_direct_routes.py", "PASS" if direct["returncode"] == 0 else "FAIL", str(reason)))
    if p2p:
        lines += ["## Point to point (own request)", "", f"`{p2p['case']}`: status {p2p['status']}, {p2p['ms']} ms, source {p2p.get('source')}, routes {p2p.get('routes')}, problems {p2p['problems']}", ""]
        rows.append(("Point to point (own)", "PASS" if p2p["passed"] else "FAIL", f"{p2p['case']}: {[r['distance_miles'] for r in p2p.get('routes', [])]} mi, {p2p['ms']} ms"))
    for label, record in presentation.items():
        report = load_json(out / "presentation" / label / "presentation-check.json", {})
        rows.append((f"presentation_check.py ({label})", "PASS" if report.get("passed") else "FAIL",
                     f"{len(report.get('cases', []))} route cases; error: {report.get('error')}"))

    table = ["## Criteria", "", "| Criterion | Result | Numbers |", "|---|---|---|"] + [f"| {a} | {b} | {c} |" for a, b, c in rows] + [""]
    split = next(i for i, line in enumerate(lines) if line.startswith("## US10"))
    text = "\n".join(lines[:split] + table + lines[split:]) + "\n"
    (out / "summary.md").write_text(text)
    return text


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base", default=DEFAULT_BASE)
    parser.add_argument("--output", type=Path, default=ROOT / "output" / "deployed-validation")
    parser.add_argument("--phases", default=",".join(PHASES))
    parser.add_argument("--pause", type=float, default=1.0, help="seconds between requests")
    parser.add_argument("--budget", type=int, default=250, help="maximum requests across all invocations")
    parser.add_argument("--summarize", action="store_true", help="only rebuild summary.md from saved results")
    args = parser.parse_args(argv)
    out = args.output
    out.mkdir(parents=True, exist_ok=True)
    if not args.summarize:
        client = Client(args.base, out, args.pause, args.budget)
        functions = {"meta": phase_meta, "cville": phase_cville, "metros": phase_metros, "errors": phase_errors,
                     "us8": phase_us8, "drive": phase_drive, "p2p": phase_p2p, "presentation": phase_presentation, "direct": phase_direct}
        try:
            for phase in args.phases.split(","):
                if phase == "ui":
                    phase_ui(out)
                else:
                    functions[phase](client, out)
        except Stop as stop:
            print(f"STOPPED: {stop}", file=sys.stderr)
    print(summarize(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
