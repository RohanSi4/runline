"""Measure RunLine against the acceptance criteria in the Project Proposal & Plan.

    PYTHONPATH=$PWD/src .venv/bin/python scripts/acceptance.py --data <release dir>

``--data`` is a directory holding ``graphs/`` (with its manifest) and
``pools/`` for the build being measured, for example the staged unified
release. Nothing in it is modified. The run checks that the pool file was
built from that exact graph before measuring anything.

Every request is planned two ways on the same graph:

* ``live``: the planner with pools switched off, the deterministic benchmark.
* ``served``: ``planner.plan_routes`` as the API calls it, pools first and
  live planning when no pool can answer. This is what users get.

The suite is the eight public starts ``scripts/bench.py`` uses (the Rotunda,
the map centre and ``ring_000`` ... ``ring_300``) at 3/5/8/12 miles, three
routes, in five variants: mixed/balanced (``base``), mixed/flat, mixed/hilly,
road/balanced and trail/balanced. US3 adds a distance-only baseline planner
(edge length as the only path cost, ranking by distance error only) built here
by patching the routing module in-process; production code is not changed.

Writes ``results.json`` (one row per mode, variant, start and distance, no
coordinates) and ``summary.md`` (one table per criterion and a PASS / FAIL /
PARTIAL matrix) to ``--output`` (default ``output/acceptance``, git-ignored),
plus the sampled GPX files under ``gpx/``. Private benchmark starts are never
read. Timings are measurements on the machine running the script.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import platform
import random
import shutil
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
MILES = (3, 5, 8, 12)
TIMING_MILES = (3, 5)
# variant -> (surface, elevation)
VARIANTS = {
    "base": ("mixed", "balanced"),
    "flat": ("mixed", "flat"),
    "hilly": ("mixed", "hilly"),
    "road": ("road", "balanced"),
    "trail": ("trail", "balanced"),
}
BASELINE = "distance_only"
MODES = ("live", "served")
PUBLIC_IDS = ("rotunda", "map_center", *(f"ring_{bearing:03d}" for bearing in range(0, 360, 60)))
LABELS = {"rotunda": "Rotunda", "map_center": "map centre"}
GPX_NS = "http://www.topografix.com/GPX/1/1"
XSD_URL = "http://www.topografix.com/GPX/1/1/gpx.xsd"

# Thresholds, as committed in the proposal.
US1_WITHIN, US1_SHARE, US1_ALL_WITHIN = 5.0, 0.80, 10.0
US2_OVERLAP, US2_SHARE = 0.80, 0.80
US3_REDUCTION, US3_MAX_ERROR = 0.20, 5.0
US5_SHARE = 0.75
US6_SHARE = 0.80
US7_SAMPLE, US7_LENGTH = 10, 1.0
US10_MEDIAN, US10_P95 = 10.0, 20.0


def label(origin_id: str) -> str:
    return LABELS.get(origin_id, origin_id)


def median(values) -> float | None:
    values = list(values)
    return statistics.median(values) if values else None


def percentile(values: list[float], fraction: float) -> float | None:
    """Linear-interpolated percentile, as scripts/bench.py computes it."""

    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def git(*arguments: str) -> str:
    try:
        return subprocess.run(["git", *arguments], cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


# --------------------------------------------------------------------------
# Data checks and the public starts


def check_data(graphs: Path, pools: Path) -> dict:
    manifest = json.loads((graphs / "manifest.json").read_text())
    area = manifest["areas"][0]
    graph_path = graphs / area["file"]
    pool_path = pools / f"{area['slug']}.sqlite"
    graph_hash = sha256(graph_path)
    database = sqlite3.connect(f"file:{pool_path}?mode=ro", uri=True)
    try:
        meta = {key: json.loads(value) for key, value in database.execute("SELECT key, value FROM meta")}
        pool_rows = database.execute("SELECT COUNT(*) FROM pools").fetchone()[0]
    finally:
        database.close()
    info = {
        "area": area["slug"],
        "graph_sha256": graph_hash,
        "graph_fingerprint": graph_hash[:16],
        "pool_graph_fingerprint": meta.get("graph"),
        "pool_format": meta.get("format"),
        "pool_build_commit": meta.get("commit"),
        "pool_surfaces": meta.get("surfaces"),
        "pool_rows": pool_rows,
        "pool_sha256": sha256(pool_path),
    }
    info["fingerprint_match"] = info["pool_graph_fingerprint"] == info["graph_fingerprint"]
    release = graphs.parent / "release.json"
    if release.exists():
        recorded = json.loads(release.read_text())
        info["release_graph_match"] = recorded.get("graph") == graph_hash
        info["release_pools_match"] = recorded.get("pools") == info["pool_sha256"]
    return info


def public_origins() -> list[dict]:
    """The eight public starts scripts/bench.py builds, without its private file."""

    sys.path.insert(0, str(ROOT / "scripts"))
    from bench import origins_from_private

    with tempfile.TemporaryDirectory() as empty:
        origins, _ = origins_from_private(Path(empty) / "none.json")
    origins = [entry for entry in origins if entry["id"] in PUBLIC_IDS]
    if [entry["id"] for entry in origins] != list(PUBLIC_IDS):
        raise SystemExit("bench.py's public starts changed; update PUBLIC_IDS")
    return origins


# --------------------------------------------------------------------------
# Planning


def build_runtime(cache: Path):
    """Imports done after the environment points at the measured data."""

    import networkx as nx

    from audit_crossings import geometric_crossings
    from runline import coverage, osm, planner, serving
    from runline.gpx import candidate_to_gpx
    from runline.geo import distance_meters
    from runline.models import Coordinate, ElevationPreference, RoutePreferences, SurfacePreference

    def no_network(*_args, **_kwargs):
        raise RuntimeError("elevation fetch attempted; the measured graph should have baked elevation")

    planner.enrich_graph_open_meteo = no_network

    @contextlib.contextmanager
    def pools_off():
        original = planner.serve_from_pool
        planner.serve_from_pool = lambda *_: None
        try:
            yield
        finally:
            planner.serve_from_pool = original

    def collapse_by_length(graph, preferences):
        """collapse_graph with edge length as the only cost: no signal,
        major-road, crossing-node or surface penalties."""

        collapsed = nx.DiGraph()
        collapsed.graph.update(graph.graph)
        collapsed.add_nodes_from(graph.nodes(data=True))
        for u, v, _, raw in osm._iter_edges(graph):
            data = dict(raw)
            data["_cost"] = float(data.get("length", 1.0))
            existing = collapsed.get_edge_data(u, v)
            if existing is None or data["_cost"] < existing["_cost"]:
                collapsed.add_edge(u, v, **data)
        return collapsed

    def rank_by_error(candidates, preferences, weights=None):
        """Ranking by absolute distance error only (ties by generation order)."""

        target = preferences.target_distance_miles
        for candidate in candidates:
            error = abs(candidate.metrics.distance_miles - target)
            candidate.score = error
            candidate.score_components = {"distance": error}
        return sorted(candidates, key=lambda candidate: (candidate.score, candidate.route_id))

    @contextlib.contextmanager
    def distance_only():
        saved = (osm.collapse_graph, osm._edge_preference_cost, osm.rank_candidates, planner.rank_candidates)
        # The prepared-graph cache is keyed by origin, radius and surface, not
        # by cost function, so it must not leak graphs across the patch.
        osm._PREPARED_CACHE.clear()
        osm.collapse_graph = collapse_by_length
        osm._edge_preference_cost = lambda data, *_args, **_kwargs: float(data.get("length", 1.0))
        osm.rank_candidates = rank_by_error
        planner.rank_candidates = rank_by_error
        try:
            with pools_off():
                yield
        finally:
            osm.collapse_graph, osm._edge_preference_cost, osm.rank_candidates, planner.rank_candidates = saved
            osm._PREPARED_CACHE.clear()

    def preferences(miles: float, variant: str):
        surface, elevation = VARIANTS["base" if variant == BASELINE else variant]
        return RoutePreferences(miles, surface=SurfacePreference(surface), elevation=ElevationPreference(elevation))

    def plan(origin, prefs, mode: str):
        context = pools_off() if mode == "live" else contextlib.nullcontext()
        started = time.perf_counter()
        try:
            with context:
                # Same call the API makes for a loop request.
                result = planner.plan_routes(origin, prefs, cache_directory=cache, start_candidate_limit=2)
        except coverage.CoverageError as error:
            return None, time.perf_counter() - started, str(error)
        return result, time.perf_counter() - started, None

    def route_graph(origin, prefs, result):
        """The graph the returned node ids were measured on."""

        if result.source == "pool":
            area = coverage.containing_area(origin)
            return serving._route_graph(
                coverage.load_area_graph(area), [candidate.node_ids for candidate in result.candidates], prefs
            )
        # A cache hit: plan_routes prepared this exact graph a moment ago.
        return osm.prepare_core(origin, osm.graph_radius_meters(prefs), cache, prefs)

    def graph_coverage_limited(origin, prefs) -> bool:
        rounded = Coordinate(round(origin.latitude, osm.ORIGIN_ROUNDING), round(origin.longitude, osm.ORIGIN_ROUNDING))
        padded = osm.graph_radius_meters(prefs) + osm.ORIGIN_ROUNDING_MARGIN_METERS
        return coverage.find_area(rounded, padded) is None

    return {
        "Coordinate": Coordinate,
        "osm": osm,
        "coverage": coverage,
        "plan": plan,
        "route_graph": route_graph,
        "preferences": preferences,
        "distance_only": distance_only,
        "geometric_crossings": geometric_crossings,
        "candidate_to_gpx": candidate_to_gpx,
        "distance_meters": distance_meters,
        "graph_coverage_limited": graph_coverage_limited,
    }


# --------------------------------------------------------------------------
# Measurement of one request


def overlap_pairs(osm, graph, candidates, target: float, tolerance: float) -> list[dict]:
    """Every pair of returned routes: shared edge length over the shorter
    route's edge length (the proposal's measure), and shared over union
    (the planner's own duplicate test, osm._distinct_by_length)."""

    edges = [osm._edge_lengths(graph, candidate.node_ids) for candidate in candidates]
    pairs = []
    for i in range(len(candidates)):
        for j in range(i + 1, len(candidates)):
            a, b = edges[i], edges[j]
            shared = sum(min(length, b.get(edge, 0.0)) for edge, length in a.items())
            total_a, total_b = sum(a.values()), sum(b.values())
            union = total_a + total_b - shared
            pairs.append({
                "pair": [i + 1, j + 1],
                "shared_over_shorter": shared / min(total_a, total_b) if min(total_a, total_b) else 0.0,
                "shared_over_union": shared / union if union else 0.0,
                "both_in_band": all(
                    abs(candidates[k].metrics.distance_miles - target) <= tolerance for k in (i, j)
                ),
            })
    return pairs


def gpx_structure(runtime, text: str, candidate) -> dict:
    """Python XML parse plus the structural checks the GPX 1.1 schema implies."""

    check = {"parsed": False}
    try:
        root = ET.fromstring(text)
    except ET.ParseError as error:
        check["parse_error"] = str(error)
        return check
    check["parsed"] = True
    ns = {"g": GPX_NS}
    tracks = root.findall("g:trk", ns)
    segments = tracks[0].findall("g:trkseg", ns) if tracks else []
    points = segments[0].findall("g:trkpt", ns) if segments else []
    parsed = []
    in_range = True
    for point in points:
        try:
            latitude, longitude = float(point.get("lat")), float(point.get("lon"))
        except (TypeError, ValueError):
            in_range = False
            continue
        in_range &= -90 <= latitude <= 90 and -180 <= longitude <= 180
        parsed.append((latitude, longitude))
    expected = candidate.coordinates
    Coordinate, distance_meters = runtime["Coordinate"], runtime["distance_meters"]
    gpx_meters = sum(
        distance_meters(Coordinate(*a), Coordinate(*b)) for a, b in zip(parsed, parsed[1:])
    )
    reported_meters = candidate.metrics.distance_miles * 1609.344
    check.update({
        "root_ok": root.tag == f"{{{GPX_NS}}}gpx" and root.get("version") == "1.1",
        "one_track_one_segment": len(tracks) == 1 and len(segments) == 1,
        "points": len(points),
        "points_match_geometry": len(parsed) == len(expected) and all(
            abs(lat - c.latitude) <= 1e-7 and abs(lon - c.longitude) <= 1e-7
            for (lat, lon), c in zip(parsed, expected)
        ),
        "lat_lon_in_range": in_range and len(parsed) == len(points),
        "closed": bool(parsed) and parsed[0] == parsed[-1],
        "length_error_pct": abs(gpx_meters - reported_meters) / reported_meters * 100 if reported_meters else None,
    })
    check["structure_ok"] = all(check[key] for key in (
        "parsed", "root_ok", "one_track_one_segment", "points_match_geometry", "lat_lon_in_range", "closed"
    )) and check["points"] >= 2
    return check


def measure(runtime, entry, miles, variant, mode, gpx_dir: Path) -> dict:
    origin = runtime["Coordinate"](float(entry["latitude"]), float(entry["longitude"]))
    prefs = runtime["preferences"](miles, variant)
    plan_mode = "live" if variant == BASELINE else mode
    result, seconds, error = runtime["plan"](origin, prefs, plan_mode)
    row = {
        "mode": mode,
        "variant": variant,
        "start": label(entry["id"]),
        "miles": miles,
        "surface": prefs.surface.value,
        "elevation": prefs.elevation.value,
        "seconds": seconds,
        "graph_coverage_limited": runtime["graph_coverage_limited"](origin, prefs),
    }
    if error is not None or not result or not result.candidates:
        row.update({"success": False, "error": error or "no routes", "routes": []})
        return row
    graph = runtime["route_graph"](origin, prefs, result)
    tolerance = prefs.distance_tolerance_miles
    routes = []
    for index, candidate in enumerate(result.candidates, start=1):
        metrics = candidate.metrics
        geometric = runtime["geometric_crossings"](graph, list(candidate.node_ids))
        text = runtime["candidate_to_gpx"](candidate, name=f"Runline option {index}")
        name = f"{mode}-{variant}-{entry['id']}-{miles}-{index}.gpx"
        (gpx_dir / name).write_text(text, encoding="utf-8")
        error_miles = abs(metrics.distance_miles - miles)
        routes.append({
            "rank": index,
            "distance_miles": metrics.distance_miles,
            "error_miles": error_miles,
            "error_pct": error_miles / miles * 100,
            "in_band": error_miles <= tolerance,
            "signals": metrics.traffic_signal_events,
            "crossings_proxy": metrics.major_crossing_events,
            "crossings_geometric": geometric,
            "interruptions_proxy": metrics.traffic_signal_events + metrics.major_crossing_events,
            "interruptions_geometric": metrics.traffic_signal_events + geometric,
            "trail_fraction": metrics.trail_fraction,
            "gain_feet": metrics.elevation_gain_feet,
            "elevation_coverage": metrics.elevation_coverage,
            "repeated_fraction": metrics.repeated_fraction,
            "compactness": metrics.compactness,
            "gpx_file": name,
            "gpx": gpx_structure(runtime, text, candidate),
        })
    row.update({
        "success": True,
        "source": result.source,
        "warnings": list(result.warnings),
        "coverage_limited_warning": any("coverage is limited" in warning for warning in result.warnings),
        "routes": routes,
        "overlap_pairs": overlap_pairs(runtime["osm"], graph, result.candidates, miles, tolerance),
    })
    return row


# --------------------------------------------------------------------------
# GPX schema validation


def fetch_xsd(directory: Path) -> tuple[Path | None, str]:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "gpx.xsd"
    if not path.exists():
        try:
            request = urllib.request.Request(XSD_URL, headers={"User-Agent": "runline-acceptance"})
            with urllib.request.urlopen(request, timeout=30) as response:
                path.write_bytes(response.read())
        except OSError as error:
            return None, f"download failed: {error}"
    try:
        ET.parse(path)
    except ET.ParseError as error:
        return None, f"downloaded schema does not parse: {error}"
    return path, "ok"


def xsd_validate(files: list[Path], xsd: Path | None) -> tuple[dict[str, bool], str]:
    """Validate against the GPX 1.1 XSD with lxml if installed, else xmllint (libxml2)."""

    if xsd is None:
        return {}, "no schema"
    try:
        from lxml import etree

        schema = etree.XMLSchema(etree.parse(str(xsd)))
        return {path.name: schema.validate(etree.parse(str(path))) for path in files}, f"lxml {etree.__version__}"
    except ImportError:
        pass
    xmllint = shutil.which("xmllint")
    if xmllint is None:
        return {}, "neither lxml nor xmllint available"
    version = subprocess.run([xmllint, "--version"], capture_output=True, text=True).stderr.splitlines()[0]
    version = version.split("using ")[-1].strip()
    results: dict[str, bool] = {}
    for start in range(0, len(files), 200):
        chunk = files[start:start + 200]
        output = subprocess.run(
            [xmllint, "--noout", "--nonet", "--schema", str(xsd), *map(str, chunk)],
            capture_output=True, text=True,
        ).stderr
        for path in chunk:
            results[path.name] = f"{path} validates" in output
    return results, f"xmllint ({version}), lxml not installed in this environment"


# --------------------------------------------------------------------------
# Criteria


def rows_for(rows, mode, variant):
    return [row for row in rows if row["mode"] == mode and row["variant"] == variant]


def keyed(rows):
    return {(row["start"], row["miles"]): row for row in rows}


def top(row):
    return row["routes"][0] if row.get("success") and row["routes"] else None


def share(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def verdict(passed: bool | None) -> str:
    return "N/A" if passed is None else ("PASS" if passed else "FAIL")


def combined(verdicts: list[str]) -> str:
    known = [item for item in verdicts if item != "N/A"]
    if not known:
        return "N/A"
    if all(item == "PASS" for item in known):
        return "PASS"
    if all(item == "FAIL" for item in known):
        return "FAIL"
    return "PARTIAL"


def us1(rows, mode):
    base = rows_for(rows, mode, "base")
    ok = [row for row in base if row["success"]]
    within5 = [row for row in ok if top(row)["error_pct"] <= US1_WITHIN]
    within10 = [row for row in ok if top(row)["error_pct"] <= US1_ALL_WITHIN]
    any10 = [row for row in ok if any(route["error_pct"] <= US1_ALL_WITHIN for route in row["routes"])]
    rate = share(len(within5), len(ok))
    passed = bool(ok) and rate >= US1_SHARE and len(within10) == len(ok)
    return {
        "requests": len(base), "successful": len(ok),
        "top_within_5pct": len(within5), "top_within_5pct_rate": rate,
        "top_within_10pct": len(within10), "any_route_within_10pct": len(any10),
        "median_top_error_pct": median(top(row)["error_pct"] for row in ok),
        "max_top_error_pct": max((top(row)["error_pct"] for row in ok), default=None),
        "outside_5pct": [f"{row['start']} {row['miles']} mi ({top(row)['error_pct']:.1f}%)" for row in ok
                         if top(row)["error_pct"] > US1_WITHIN],
        "verdict": verdict(passed),
    }


def us2(rows, mode):
    ok = [row for row in rows_for(rows, mode, "base") if row["success"]]
    multi = [row for row in ok if len(row["routes"]) >= 2]

    def distinct(row, in_band=False):
        return any(pair["shared_over_shorter"] <= US2_OVERLAP and (pair["both_in_band"] or not in_band)
                   for pair in row["overlap_pairs"])

    hits = [row for row in ok if distinct(row)]
    in_band_hits = [row for row in ok if distinct(row, True)]
    rate = share(len(hits), len(ok))
    lowest = [min(pair["shared_over_shorter"] for pair in row["overlap_pairs"]) for row in multi]
    return {
        "applicable": len(ok), "returned_two_or_more": len(multi),
        "with_distinct_pair": len(hits), "rate": rate,
        "with_distinct_in_band_pair": len(in_band_hits),
        "rate_over_two_or_more": share(len(hits), len(multi)),
        "median_lowest_pair_overlap": median(lowest),
        "max_lowest_pair_overlap": max(lowest, default=None),
        "median_all_pair_overlap": median(pair["shared_over_shorter"] for row in multi for pair in row["overlap_pairs"]),
        "failing": [f"{row['start']} {row['miles']} mi ({len(row['routes'])} routes)" for row in ok if not distinct(row)],
        "verdict": verdict(bool(ok) and rate >= US2_SHARE),
    }


def us3(rows, mode):
    aware = keyed(row for row in rows_for(rows, mode, "base") if row["success"])
    baseline = keyed(row for row in rows_for(rows, "live", BASELINE) if row["success"])
    keys = sorted(set(aware) & set(baseline), key=lambda key: (PUBLIC_ORDER.get(key[0], 99), key[1]))
    result = {"paired_requests": len(keys)}
    if not keys:
        result["verdict"] = "N/A"
        return result
    aware_error = median(top(aware[key])["error_pct"] for key in keys)
    result["aware_median_top_error_pct"] = aware_error
    result["baseline_median_top_error_pct"] = median(top(baseline[key])["error_pct"] for key in keys)
    measures = {}
    for measure in ("proxy", "geometric"):
        field = f"interruptions_{measure}"
        aware_top = [top(aware[key])[field] for key in keys]
        base_top = [top(baseline[key])[field] for key in keys]
        aware_all = [route[field] for key in keys for route in aware[key]["routes"]]
        base_all = [route[field] for key in keys for route in baseline[key]["routes"]]

        def reduction(aware_values, base_values):
            a, b = median(aware_values), median(base_values)
            return None if not b else (b - a) / b

        top_reduction = reduction(aware_top, base_top)
        measures[measure] = {
            "aware_median_top": median(aware_top), "baseline_median_top": median(base_top),
            "top_reduction": top_reduction,
            "aware_median_all": median(aware_all), "baseline_median_all": median(base_all),
            "all_reduction": reduction(aware_all, base_all),
            "aware_total_top": sum(aware_top), "baseline_total_top": sum(base_top),
            "aware_mean_all": statistics.fmean(aware_all), "baseline_mean_all": statistics.fmean(base_all),
            "cases_fewer": sum(a < b for a, b in zip(aware_top, base_top)),
            "cases_equal": sum(a == b for a, b in zip(aware_top, base_top)),
            "cases_more": sum(a > b for a, b in zip(aware_top, base_top)),
            "verdict": verdict(top_reduction is not None and top_reduction >= US3_REDUCTION
                               and aware_error <= US3_MAX_ERROR),
        }
    result["measures"] = measures
    result["by_miles"] = {
        miles: {
            measure: {
                "aware_median_top": median(top(aware[key])[f"interruptions_{measure}"] for key in keys if key[1] == miles),
                "baseline_median_top": median(top(baseline[key])[f"interruptions_{measure}"] for key in keys if key[1] == miles),
            } for measure in ("proxy", "geometric")
        } | {
            "aware_median_top_error_pct": median(top(aware[key])["error_pct"] for key in keys if key[1] == miles),
            "baseline_median_top_error_pct": median(top(baseline[key])["error_pct"] for key in keys if key[1] == miles),
        }
        for miles in MILES
    }
    result["verdict"] = combined([measures["proxy"]["verdict"], measures["geometric"]["verdict"]])
    return result


def us5(rows, mode):
    neutral = keyed(row for row in rows_for(rows, mode, "base") if row["success"])
    out = {"directions": {}}
    total = strict_total = applicable_total = 0
    for variant, toward in (("trail", 1), ("road", -1)):
        requested = keyed(row for row in rows_for(rows, mode, variant) if row["success"])
        applicable = [key for key in neutral if key in requested and 0 < top(neutral[key])["trail_fraction"] < 1]
        moved = strict = same_route = 0
        misses = []
        for key in applicable:
            delta = (top(requested[key])["trail_fraction"] - top(neutral[key])["trail_fraction"]) * toward
            moved += delta >= -1e-12
            strict += delta > 1e-12
            same_route += abs(delta) <= 1e-12
            if delta < -1e-12:
                misses.append(f"{key[0]} {key[1]} mi ({top(neutral[key])['trail_fraction']:.2f} -> "
                              f"{top(requested[key])['trail_fraction']:.2f})")
        out["directions"][variant] = {
            "applicable": len(applicable), "toward_or_tie": moved, "strictly_toward": strict, "ties": same_route,
            "rate": share(moved, len(applicable)), "strict_rate": share(strict, len(applicable)), "away": misses,
            "median_trail_fraction_requested": median(top(requested[key])["trail_fraction"] for key in applicable),
            "median_trail_fraction_neutral": median(top(neutral[key])["trail_fraction"] for key in applicable),
        }
        total += moved
        strict_total += strict
        applicable_total += len(applicable)
    out["neutral_cases"] = len(neutral)
    out["neutral_both_surfaces"] = sum(0 < top(row)["trail_fraction"] < 1 for row in neutral.values())
    out.update({"applicable": applicable_total, "toward_or_tie": total, "strictly_toward": strict_total,
                "rate": share(total, applicable_total), "strict_rate": share(strict_total, applicable_total)})
    out["verdict"] = verdict(None if not applicable_total else out["rate"] >= US5_SHARE)
    return out


def us6(rows, mode):
    flat = keyed(row for row in rows_for(rows, mode, "flat") if row["success"])
    hilly = keyed(row for row in rows_for(rows, mode, "hilly") if row["success"])
    pairs = [key for key in flat if key in hilly
             and top(flat[key])["elevation_coverage"] >= 0.95 and top(hilly[key])["elevation_coverage"] >= 0.95]
    greater = [key for key in pairs if top(hilly[key])["gain_feet"] > top(flat[key])["gain_feet"] + 1e-9]
    ties = [key for key in pairs if abs(top(hilly[key])["gain_feet"] - top(flat[key])["gain_feet"]) <= 1e-9]
    less = [key for key in pairs if key not in greater and key not in ties]
    rate = share(len(greater), len(pairs))
    return {
        "pairs": len(pairs), "hilly_greater": len(greater), "ties": len(ties), "hilly_less": len(less),
        "rate": rate, "rate_with_ties": share(len(greater) + len(ties), len(pairs)),
        "median_gain_flat_ft": median(top(flat[key])["gain_feet"] for key in pairs),
        "median_gain_hilly_ft": median(top(hilly[key])["gain_feet"] for key in pairs),
        "median_gain_balanced_ft": median(top(row)["gain_feet"] for row in rows_for(rows, mode, "base") if row["success"]),
        "not_greater": [f"{key[0]} {key[1]} mi ({top(flat[key])['gain_feet']:.0f} vs {top(hilly[key])['gain_feet']:.0f} ft)"
                        for key in pairs if key not in greater],
        "verdict": verdict(None if not pairs else rate >= US6_SHARE),
    }


def us7(rows, mode, xsd_results, sample_names):
    routes = [route for row in rows if row["mode"] == mode and row["success"] for route in row["routes"]]

    def ok(route):
        check = route["gpx"]
        schema = xsd_results.get(route["gpx_file"]) if xsd_results else True
        return (check["parsed"] and check["structure_ok"] and schema
                and check["length_error_pct"] is not None and check["length_error_pct"] <= US7_LENGTH)

    sample = [route for route in routes if route["gpx_file"] in sample_names]
    summary = {}
    for name, chosen in (("sample", sample), ("all", routes)):
        summary[name] = {
            "routes": len(chosen),
            "parsed": sum(route["gpx"]["parsed"] for route in chosen),
            "xsd_valid": sum(bool(xsd_results.get(route["gpx_file"])) for route in chosen) if xsd_results else None,
            "structure_ok": sum(route["gpx"]["structure_ok"] for route in chosen),
            "closed": sum(route["gpx"].get("closed", False) for route in chosen),
            "length_within_1pct": sum((route["gpx"]["length_error_pct"] or 99) <= US7_LENGTH for route in chosen),
            "max_length_error_pct": max((route["gpx"]["length_error_pct"] or 0 for route in chosen), default=None),
            "all_ok": sum(ok(route) for route in chosen),
        }
    summary["sample_files"] = sorted(route["gpx_file"] for route in sample)
    summary["verdict"] = verdict(bool(sample) and summary["sample"]["all_ok"] == len(sample) == US7_SAMPLE)
    return summary


def us10(samples, mode):
    chosen = [sample for sample in samples if sample["mode"] == mode]
    first = [sample["seconds"] for sample in chosen if sample["repeat"] == 0]
    repeat = [sample["seconds"] for sample in chosen if sample["repeat"] > 0]
    every = first + repeat
    out = {
        "samples": len(every),
        "median_s": median(every), "p95_s": percentile(every, 0.95), "max_s": max(every, default=None),
        "first_median_s": median(first), "first_p95_s": percentile(first, 0.95),
        "repeat_median_s": median(repeat), "repeat_p95_s": percentile(repeat, 0.95),
    }
    for miles in TIMING_MILES:
        values = [sample["seconds"] for sample in chosen if sample["miles"] == miles]
        out[f"{miles}mi_median_s"], out[f"{miles}mi_p95_s"] = median(values), percentile(values, 0.95)
    for source in ("pool", "live"):
        values = [sample["seconds"] for sample in chosen if sample.get("source") == source]
        out[f"{source}_samples"], out[f"{source}_median_s"], out[f"{source}_p95_s"] = (
            len(values), median(values), percentile(values, 0.95))
    passed = bool(every) and out["median_s"] < US10_MEDIAN and out["p95_s"] < US10_P95
    out["verdict"] = verdict(passed)
    return out


PUBLIC_ORDER = {label(origin_id): index for index, origin_id in enumerate(PUBLIC_IDS)}


# --------------------------------------------------------------------------
# Markdown


def fmt(value, digits=2, percent=False):
    if value is None:
        return "n/a"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if percent:
        return f"{value * 100:.{max(digits - 1, 0)}f}%"
    if isinstance(value, int):
        return str(value)
    return f"{value:.{digits}f}"


def table(headers, body) -> str:
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" if i == 0 else "---:" for i in range(len(headers))) + "|"]
    lines += ["| " + " | ".join(str(cell) for cell in row) + " |" for row in body]
    return "\n".join(lines)


def render(report) -> str:
    c = report["criteria"]
    meta = report["meta"]
    out = [
        "# RunLine acceptance results",
        "",
        f"Run {meta['date']} on code `{meta['commit']}`{' (dirty tree)' if meta['dirty'] else ''}, "
        f"graph `{meta['data']['graph_fingerprint']}` (pool fingerprint `{meta['data']['pool_graph_fingerprint']}`, "
        f"match: {fmt(meta['data']['fingerprint_match'])}). {meta['machine']}. Timings are laptop measurements.",
        "",
        "Modes: **live** = planner with pools off (deterministic benchmark); **served** = pools first, live "
        "fallback, as the API serves. 8 public starts x 3/5/8/12 mi = 32 requests per variant.",
        "",
    ]

    out += ["## Matrix", ""]
    out.append(table(
        ["Criterion", "Threshold", "Live", "Served", "Overall"],
        [[row["criterion"], row["threshold"], row["live"], row["served"], f"**{row['overall']}**"] for row in report["matrix"]],
    ))

    out += ["", "## US1 distance accuracy (mixed, balanced)", ""]
    out.append(table(
        ["Mode", "Successful", "Top within 5%", "Rate", "Top within 10%", "Median error", "Max error"],
        [[mode, f"{c['US1'][mode]['successful']}/{c['US1'][mode]['requests']}",
          c["US1"][mode]["top_within_5pct"], fmt(c["US1"][mode]["top_within_5pct_rate"], 2, True),
          f"{c['US1'][mode]['top_within_10pct']}/{c['US1'][mode]['successful']}",
          fmt(c["US1"][mode]["median_top_error_pct"]) + "%", fmt(c["US1"][mode]["max_top_error_pct"]) + "%"] for mode in MODES],
    ))
    for mode in MODES:
        if c["US1"][mode]["outside_5pct"]:
            out.append(f"\n{mode} top routes outside 5%: " + ", ".join(c["US1"][mode]["outside_5pct"]))

    out += ["", "## US2 distinct alternatives (overlap = shared edge length / shorter route's edge length)", ""]
    out.append(table(
        ["Mode", "Applicable", "Returned >= 2", "Pair <= 80%", "Rate", "Pair <= 80%, both in band",
         "Median lowest pair overlap", "Max lowest pair overlap"],
        [[mode, c["US2"][mode]["applicable"], c["US2"][mode]["returned_two_or_more"], c["US2"][mode]["with_distinct_pair"],
          fmt(c["US2"][mode]["rate"], 2, True), c["US2"][mode]["with_distinct_in_band_pair"],
          fmt(c["US2"][mode]["median_lowest_pair_overlap"], 3), fmt(c["US2"][mode]["max_lowest_pair_overlap"], 3)]
         for mode in MODES],
    ))
    for mode in MODES:
        if c["US2"][mode]["failing"]:
            out.append(f"\n{mode} without a distinct pair: " + ", ".join(c["US2"][mode]["failing"]))

    out += ["", "## US3 interruptions vs distance-only baseline (signals + major-road crossings)", ""]
    body = []
    for mode in MODES:
        result = c["US3"][mode]
        for measure in ("proxy", "geometric"):
            m = result["measures"][measure]
            body.append([
                mode, measure, result["paired_requests"],
                fmt(m["baseline_median_top"], 1), fmt(m["aware_median_top"], 1), fmt(m["top_reduction"], 2, True),
                fmt(m["baseline_median_all"], 1), fmt(m["aware_median_all"], 1), fmt(m["all_reduction"], 2, True),
                f"{m['baseline_total_top']} -> {m['aware_total_top']}",
                f"{m['cases_fewer']}/{m['cases_equal']}/{m['cases_more']}",
                fmt(result["aware_median_top_error_pct"]) + "%", m["verdict"],
            ])
    out.append(table(
        ["Mode", "Crossings", "Pairs", "Baseline median (top)", "Aware median (top)", "Reduction (top)",
         "Baseline median (all)", "Aware median (all)", "Reduction (all)", "Total top", "Fewer/equal/more",
         "Aware median error", "Verdict"], body))
    out.append(f"\nBaseline median top-route distance error: {fmt(c['US3']['live']['baseline_median_top_error_pct'])}%.")
    out += ["", "Per distance, live, top route medians (baseline -> aware):", ""]
    out.append(table(
        ["Miles", "Proxy", "Geometric", "Error % baseline -> aware"],
        [[miles, f"{fmt(v['proxy']['baseline_median_top'], 1)} -> {fmt(v['proxy']['aware_median_top'], 1)}",
          f"{fmt(v['geometric']['baseline_median_top'], 1)} -> {fmt(v['geometric']['aware_median_top'], 1)}",
          f"{fmt(v['baseline_median_top_error_pct'])} -> {fmt(v['aware_median_top_error_pct'])}"]
         for miles, v in c["US3"]["live"]["by_miles"].items()],
    ))

    out += ["", "## US5 surface preference (top route trail fraction vs mixed)", ""]
    body = []
    for mode in MODES:
        for direction, values in c["US5"][mode]["directions"].items():
            body.append([mode, direction, values["applicable"], values["toward_or_tie"], values["strictly_toward"],
                         values["ties"], fmt(values["rate"], 2, True), fmt(values["strict_rate"], 2, True),
                         f"{fmt(values['median_trail_fraction_neutral'])} -> {fmt(values['median_trail_fraction_requested'])}"])
        total = c["US5"][mode]
        body.append([mode, "**both**", total["applicable"], total["toward_or_tie"], total["strictly_toward"],
                     total["toward_or_tie"] - total["strictly_toward"], fmt(total["rate"], 2, True),
                     fmt(total["strict_rate"], 2, True), total["verdict"]])
    out.append(table(["Mode", "Request", "Applicable", "Toward or tie", "Strictly toward", "Ties", "Rate",
                      "Strict rate", "Median trail fraction mixed -> requested / verdict"], body))
    for mode in MODES:
        away = [item for values in c["US5"][mode]["directions"].values() for item in values["away"]]
        out.append(f"\n{mode}: neutral routes with both surfaces {c['US5'][mode]['neutral_both_surfaces']}/"
                   f"{c['US5'][mode]['neutral_cases']}; moved away: " + (", ".join(away) or "none"))

    out += ["", "## US6 elevation preference (top route gain, hilly vs flat)", ""]
    out.append(table(
        ["Mode", "Pairs", "Hilly greater", "Ties", "Hilly less", "Rate", "Median gain flat / balanced / hilly, ft", "Verdict"],
        [[mode, c["US6"][mode]["pairs"], c["US6"][mode]["hilly_greater"], c["US6"][mode]["ties"],
          c["US6"][mode]["hilly_less"], fmt(c["US6"][mode]["rate"], 2, True),
          f"{fmt(c['US6'][mode]['median_gain_flat_ft'], 0)} / {fmt(c['US6'][mode]['median_gain_balanced_ft'], 0)} / "
          f"{fmt(c['US6'][mode]['median_gain_hilly_ft'], 0)}", c["US6"][mode]["verdict"]] for mode in MODES],
    ))
    for mode in MODES:
        if c["US6"][mode]["not_greater"]:
            out.append(f"\n{mode} not greater (flat vs hilly): " + ", ".join(c["US6"][mode]["not_greater"]))

    out += ["", f"## US7 GPX validity ({report['gpx_validator']})", ""]
    body = []
    for mode in MODES:
        for scope in ("sample", "all"):
            values = c["US7"][mode][scope]
            body.append([mode, scope, values["routes"], values["parsed"], fmt(values["xsd_valid"]), values["structure_ok"],
                         values["closed"], values["length_within_1pct"], fmt(values["max_length_error_pct"], 3) + "%",
                         values["all_ok"]])
    out.append(table(["Mode", "Set", "Routes", "XML parses", "XSD valid", "Structure ok", "Closed",
                      "Length within 1%", "Max length error", "All checks"], body))

    out += ["", "## US10 warm latency, 3 and 5 mi (laptop)", ""]
    out.append(table(
        ["Mode", "Samples", "Median s", "p95 s", "Max s", "First-for-start median / p95", "Repeat median / p95",
         "Pool hits median", "Live median", "Verdict"],
        [[mode, c["US10"][mode]["samples"], fmt(c["US10"][mode]["median_s"], 3), fmt(c["US10"][mode]["p95_s"], 3),
          fmt(c["US10"][mode]["max_s"], 3),
          f"{fmt(c['US10'][mode]['first_median_s'], 3)} / {fmt(c['US10'][mode]['first_p95_s'], 3)}",
          f"{fmt(c['US10'][mode]['repeat_median_s'], 3)} / {fmt(c['US10'][mode]['repeat_p95_s'], 3)}",
          f"{fmt(c['US10'][mode]['pool_median_s'], 3)} ({c['US10'][mode]['pool_samples']})",
          f"{fmt(c['US10'][mode]['live_median_s'], 3)} ({c['US10'][mode]['live_samples']})",
          c["US10"][mode]["verdict"]] for mode in MODES],
    ))
    cold = report["cold"]
    out.append(f"\nProcess cold start (excluded): graph load {fmt(cold['graph_load_s'], 2)} s, first live request "
               f"{fmt(cold['first_live_s'], 2)} s, first served request {fmt(cold['first_served_s'], 2)} s.")

    out += ["", "## Per-request results (mixed, balanced; top route)", ""]
    live = keyed(rows_for(report["rows"], "live", "base"))
    served = keyed(rows_for(report["rows"], "served", "base"))
    baseline = keyed(rows_for(report["rows"], "live", BASELINE))
    body = []
    for key in sorted(live, key=lambda key: (PUBLIC_ORDER[key[0]], key[1])):
        cells = [key[0], key[1]]
        for row in (live[key], served.get(key), baseline.get(key)):
            route = top(row) if row else None
            if route is None:
                cells += ["fail", "", ""]
                continue
            cells += [f"{route['distance_miles']:.2f}", f"{route['error_pct']:.1f}%",
                      f"{route['signals']}+{route['crossings_proxy']} ({route['crossings_geometric']})"]
        row = served.get(key)
        cells.insert(5, row["source"] if row and row["success"] else "")
        flags = []
        if live[key].get("coverage_limited_warning") or (row and row.get("coverage_limited_warning")):
            flags.append("coverage_limited")
        cells.append(", ".join(flags))
        body.append(cells)
    out.append(table(
        ["Start", "mi", "Live mi", "Live err", "Live sig+cross (geom)", "Served src", "Served mi", "Served err",
         "Served sig+cross (geom)", "Baseline mi", "Baseline err", "Baseline sig+cross (geom)", "Warnings"], body))
    limited = sorted({(row["start"], row["miles"]) for row in report["rows"]
                      if row.get("coverage_limited_warning") or row.get("graph_coverage_limited")},
                     key=lambda key: (PUBLIC_ORDER[key[0]], key[1]))
    out.append("\nCoverage limited (graph crop overhangs the map, any variant): "
               + (", ".join(f"{start} {miles} mi" for start, miles in limited) or "none"))
    return "\n".join(out) + "\n"


# --------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data", type=Path, help="directory holding graphs/ and pools/ to measure")
    parser.add_argument("--output", type=Path, default=ROOT / "output/acceptance")
    parser.add_argument("--timing-repeat", type=int, default=3)
    parser.add_argument("--seed", type=int, default=4971, help="seed for the GPX sample")
    parser.add_argument("--starts", nargs="+", help="subset of public start ids, for a quick check")
    parser.add_argument("--miles", type=float, nargs="+", help="subset of distances, for a quick check")
    arguments = parser.parse_args()

    graphs = arguments.data / "graphs" if arguments.data else Path(os.environ.get("RUNLINE_GRAPHS_DIR", ""))
    pools = arguments.data / "pools" if arguments.data else Path(os.environ.get("RUNLINE_POOLS_DIR", ""))
    if not (graphs / "manifest.json").exists() or not pools.is_dir():
        parser.error("pass --data, or set RUNLINE_GRAPHS_DIR and RUNLINE_POOLS_DIR")
    data = check_data(graphs, pools)
    print(f"graph {data['graph_fingerprint']}, pool built from {data['pool_graph_fingerprint']}", file=sys.stderr)
    if not data["fingerprint_match"]:
        print("pool fingerprint does not match the graph; refusing to measure", file=sys.stderr)
        return 1
    os.environ["RUNLINE_GRAPHS_DIR"] = str(graphs)
    os.environ["RUNLINE_POOLS_DIR"] = str(pools)
    os.environ["RUNLINE_ALLOW_OSM_DOWNLOAD"] = "0"
    sys.path.insert(0, str(ROOT / "src"))
    sys.path.insert(0, str(ROOT / "scripts"))

    origins = public_origins()
    manifest_center = json.loads((graphs / "manifest.json").read_text())["areas"][0]
    shipped_center = json.loads((ROOT / "graphs/manifest.json").read_text())["areas"][0]
    if (manifest_center["latitude"], manifest_center["longitude"]) != (shipped_center["latitude"], shipped_center["longitude"]):
        print("measured graph's centre differs from the one bench.py's starts use", file=sys.stderr)
        return 1
    if arguments.starts:
        origins = [entry for entry in origins if entry["id"] in arguments.starts]
    miles_list = tuple(int(m) if m == int(m) else m for m in arguments.miles) if arguments.miles else MILES
    timing_miles = tuple(miles for miles in TIMING_MILES if miles in miles_list)

    output = arguments.output
    output.mkdir(parents=True, exist_ok=True)
    workdir = Path(tempfile.mkdtemp(prefix="runline-acceptance-"))
    gpx_dir = workdir / "gpx"
    gpx_dir.mkdir()
    runtime = build_runtime(workdir / "cache")
    osm, coverage = runtime["osm"], runtime["coverage"]
    Coordinate = runtime["Coordinate"]

    # Process cold start, excluded from US10: the first request loads the
    # area graph and checks the pool's fingerprint.
    started = time.perf_counter()
    area_graph = coverage.load_area_graph(coverage.areas()[0])
    graph_load = time.perf_counter() - started
    centre = next(entry for entry in origins + public_origins() if entry["id"] == "map_center")
    centre_origin = Coordinate(centre["latitude"], centre["longitude"])
    _, first_served, _ = runtime["plan"](centre_origin, runtime["preferences"](5, "base"), "served")
    _, first_live, _ = runtime["plan"](centre_origin, runtime["preferences"](5, "base"), "live")
    runtime["geometric_crossings"](osm.prepare_core(centre_origin, osm.graph_radius_meters(runtime["preferences"](5, "base")),
                                                    workdir / "cache", runtime["preferences"](5, "base")), [])
    cold = {"graph_load_s": graph_load, "first_served_s": first_served, "first_live_s": first_live,
            "graph_nodes": area_graph.number_of_nodes(), "graph_edges": area_graph.number_of_edges()}

    # US10: warm instance. Each start's prepared-graph cache is cleared first,
    # so repeat 0 is a new start on a warm instance and later repeats reuse it.
    timing = []
    for mode in MODES:
        for entry in origins:
            origin = Coordinate(float(entry["latitude"]), float(entry["longitude"]))
            for miles in timing_miles:
                osm._PREPARED_CACHE.clear()
                for repeat in range(arguments.timing_repeat):
                    result, seconds, error = runtime["plan"](origin, runtime["preferences"](miles, "base"), mode)
                    timing.append({
                        "mode": mode, "start": label(entry["id"]), "miles": miles, "repeat": repeat, "seconds": seconds,
                        "source": result.source if result else None, "error": error,
                        "top_miles": result.candidates[0].metrics.distance_miles if result and result.candidates else None,
                    })
                print(f"timing {mode} {entry['id']} {miles} mi: "
                      + ", ".join(f"{sample['seconds']:.2f}s" for sample in timing[-arguments.timing_repeat:]), file=sys.stderr)

    rows = []
    for mode in MODES:
        osm._PREPARED_CACHE.clear()
        for entry in origins:
            for miles in miles_list:
                for variant in VARIANTS:
                    rows.append(measure(runtime, entry, miles, variant, mode, gpx_dir))
                print(f"{mode} {entry['id']} {miles} mi done", file=sys.stderr)
    with runtime["distance_only"]():
        for entry in origins:
            for miles in miles_list:
                rows.append(measure(runtime, entry, miles, BASELINE, "live", gpx_dir))
            print(f"baseline {entry['id']} done", file=sys.stderr)

    # Repeat requests return the same routes (sanity check on determinism).
    base_top = {(row["mode"], row["start"], row["miles"]): top(row)["distance_miles"]
                for row in rows if row["variant"] == "base" and row["success"]}
    deterministic = all(sample["top_miles"] == base_top.get((sample["mode"], sample["start"], sample["miles"]))
                        for sample in timing if sample["top_miles"] is not None)

    # US7: ten sampled routes per mode, from every variant; all routes are validated too.
    xsd, xsd_status = fetch_xsd(output / "xsd")
    files = sorted(gpx_dir.glob("*.gpx"))
    xsd_results, validator = xsd_validate(files, xsd)
    validator = f"XSD: {validator}; schema {xsd_status}"
    rng = random.Random(arguments.seed)
    sample_names = set()
    sample_dir = output / "gpx"
    shutil.rmtree(sample_dir, ignore_errors=True)
    sample_dir.mkdir()
    for mode in MODES:
        names = sorted(route["gpx_file"] for row in rows if row["mode"] == mode and row["success"] for route in row["routes"])
        chosen = rng.sample(names, min(US7_SAMPLE, len(names)))
        sample_names.update(chosen)
        for name in chosen:
            shutil.copy(gpx_dir / name, sample_dir / name)

    criteria = {
        "US1": {mode: us1(rows, mode) for mode in MODES},
        "US2": {mode: us2(rows, mode) for mode in MODES},
        "US3": {mode: us3(rows, mode) for mode in MODES},
        "US5": {mode: us5(rows, mode) for mode in MODES},
        "US6": {mode: us6(rows, mode) for mode in MODES},
        "US7": {mode: us7(rows, mode, xsd_results, sample_names) for mode in MODES},
        "US10": {mode: us10(timing, mode) for mode in MODES},
    }
    thresholds = {
        "US1": ">= 80% of top routes within 5%; all within 10%",
        "US2": ">= 80% of requests with a pair overlapping <= 80%",
        "US3": "median signals + crossings (top) down >= 20%; median error <= 5%",
        "US5": ">= 75% of applicable cases toward the preference",
        "US6": "hilly gain > flat gain in >= 80% of pairs",
        "US7": "10 sampled GPX parse, validate, match length within 1%",
        "US10": "warm 3/5 mi median < 10 s, p95 < 20 s (laptop)",
    }
    matrix = []
    for name, results in criteria.items():
        verdicts = [results[mode]["verdict"] for mode in MODES]
        matrix.append({"criterion": name, "threshold": thresholds[name], "live": verdicts[0], "served": verdicts[1],
                       "overall": combined(verdicts)})

    dirty = bool(git("status", "--porcelain", "--", "src", "scripts"))
    report = {
        "meta": {
            "date": datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
            "commit": git("rev-parse", "HEAD"),
            "dirty": dirty,
            "data": data,
            "machine": f"{platform.system()} {platform.machine()}, Python {platform.python_version()}",
            "deterministic_repeats": deterministic,
            "starts": [label(entry["id"]) for entry in origins],
            "miles": list(miles_list),
            "variants": {**{name: {"surface": s, "elevation": e} for name, (s, e) in VARIANTS.items()},
                         BASELINE: "edge length only path cost, ranking by distance error only, pools off"},
            "overlap_definition": "shared undirected edge length (min of the two) / shorter route's unique edge length",
        },
        "gpx_validator": validator,
        "cold": cold,
        "matrix": matrix,
        "criteria": criteria,
        "timing": timing,
        "rows": rows,
    }
    (output / "results.json").write_text(json.dumps(report, indent=1, default=str) + "\n")
    (output / "summary.md").write_text(render(report))
    shutil.rmtree(workdir, ignore_errors=True)
    for row in matrix:
        print(f"{row['criterion']:>5}  live {row['live']:<7} served {row['served']:<7} overall {row['overall']}")
    print(f"deterministic repeats: {deterministic}; wrote {output / 'summary.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
