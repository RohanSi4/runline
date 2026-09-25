"""Offline, paired routing benchmark.

Optional .private/benchmarks.local.json format:
{"origins": [{"id": "uva_jpa", "latitude": 38.0, "longitude": -78.0}]}.
Only Charlottesville starts are run against the shipped graph.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import resource
import statistics
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    return ordered[lower] + (ordered[min(lower + 1, len(ordered) - 1)] - ordered[lower]) * (position - lower)


def origins_from_private(path: Path) -> tuple[list[dict], list[str]]:
    origins = [{"id": "rotunda", "latitude": 38.035556, "longitude": -78.503333}]
    center = json.loads((ROOT / "graphs/manifest.json").read_text())["areas"][0]
    origins.append({"id": "map_center", "latitude": center["latitude"], "longitude": center["longitude"]})
    # Public, deterministic spread: 2.5 km from the centre every 60 degrees.
    for bearing in range(0, 360, 60):
        radians, lat = math.radians(bearing), math.radians(center["latitude"])
        origins.append({
            "id": f"ring_{bearing:03d}",
            "latitude": center["latitude"] + 2_500 * math.cos(radians) / 111_320,
            "longitude": center["longitude"] + 2_500 * math.sin(radians) / (111_320 * math.cos(lat)),
        })
    public = json.loads((ROOT / "config/benchmarks.public.json").read_text())
    private = json.loads(path.read_text()) if path.exists() else {}
    entries = private.get("origins", []) if isinstance(private, dict) else private
    by_id = {entry["id"]: entry for entry in entries}
    skipped = []
    for entry in public["origins"]:
        if entry["region"] != "charlottesville":
            skipped.append(entry["id"])
            continue
        matched = by_id.get(entry["id"])
        if matched and "latitude" in matched and "longitude" in matched:
            origins.append({"id": entry["id"], "latitude": matched["latitude"], "longitude": matched["longitude"]})
        else:
            skipped.append(entry["id"])
    return origins, skipped


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=ROOT)
    parser.add_argument("--private", type=Path, default=ROOT / ".private/benchmarks.local.json")
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.repeat < 1:
        parser.error("--repeat must be positive")
    os.environ["RUNLINE_ALLOW_OSM_DOWNLOAD"] = "0"
    sys.path.insert(0, str(args.source_root / "src"))

    from runline.coverage import CoverageError
    from runline.models import Coordinate, RoutePreferences
    from runline.osm import generate_loops, graph_radius_meters, prepare_core

    sys.path.insert(0, str(ROOT / "scripts"))
    from audit_crossings import geometric_crossings

    def shared_fraction(first, second, graph):
        def lengths(route):
            return {(min(u, v), max(u, v)): float(graph.edges[u, v].get("length", 0))
                    for u, v in zip(route.node_ids, route.node_ids[1:])}
        a, b = lengths(first), lengths(second)
        shared = sum(min(length, b.get(edge, 0)) for edge, length in a.items())
        union = sum(a.values()) + sum(b.values()) - shared
        return shared / union if union else 0

    origins, skipped = origins_from_private(args.private)
    if skipped:
        print("Skipped origins: " + ", ".join(skipped), file=sys.stderr)
    cases = []
    for entry in origins:
        origin = Coordinate(float(entry["latitude"]), float(entry["longitude"]))
        for miles in (3, 5, 8, 12):
            preferences = RoutePreferences(target_distance_miles=miles)
            preparation = []
            generation = []
            result = {"origin_id": entry["id"], "target_miles": miles}
            for _ in range(args.repeat):
                start = time.perf_counter()
                try:
                    graph = prepare_core(origin, graph_radius_meters(preferences), ROOT / "cache/bench", preferences)
                except CoverageError as error:
                    result["error"] = str(error)
                    break
                prepared = time.perf_counter()
                routes = generate_loops(graph, origin, preferences)
                finished = time.perf_counter()
                preparation.append(prepared - start)
                generation.append(finished - prepared)
            if generation:
                result.update({
                    "coverage_limited": bool(graph.graph.get("coverage_limited")),
                    "feasible_top3": sum(abs(route.metrics.distance_miles - miles) <= preferences.distance_tolerance_miles for route in routes),
                    "max_shared_length_fraction": max(
                        (shared_fraction(a, b, graph) for i, a in enumerate(routes) for b in routes[i + 1:]),
                        default=0,
                    ),
                    "routes": [{
                        "distance_miles": route.metrics.distance_miles,
                        "abs_error_miles": abs(route.metrics.distance_miles - miles),
                        "signals": route.metrics.traffic_signal_events,
                        "crossings_proxy": route.metrics.major_crossing_events,
                        "crossings_geometry": geometric_crossings(graph, list(route.node_ids)),
                        "repeated_fraction": route.metrics.repeated_fraction,
                    } for route in routes],
                    "generation_p50_s": statistics.median(generation),
                    "generation_p95_s": percentile(generation, .95),
                    "end_to_end_p50_s": statistics.median([a + b for a, b in zip(preparation, generation)]),
                    "end_to_end_p95_s": percentile([a + b for a, b in zip(preparation, generation)], .95),
                    "cold_end_to_end_s": preparation[0] + generation[0],
                    "warm_generation_p50_s": statistics.median(generation[1:]) if len(generation) > 1 else None,
                    "warm_end_to_end_p50_s": statistics.median([a + b for a, b in zip(preparation[1:], generation[1:])]) if len(generation) > 1 else None,
                })
            cases.append(result)
            print(f"{entry['id']} {miles}mi: {result.get('feasible_top3', 'coverage error')} feasible", file=sys.stderr)
    report = {"repeat": args.repeat, "skipped_origins": skipped, "cases": cases,
              "peak_rss_mb": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (1024 * 1024 if sys.platform == "darwin" else 1024)}
    payload = json.dumps(report, indent=2) + "\n"
    if args.output:
        args.output.write_text(payload)
    else:
        print(payload)


if __name__ == "__main__":
    main()
