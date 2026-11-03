"""Compare answers from precomputed pools with live planning on the same requests.

    python scripts/compare_pools.py --area charlottesville-va --starts 40

Starts are random walkable nodes near the area's centre (seeded, so runs are
comparable and no coordinates live in this file). Every request is answered
both ways and the top routes compared on what a runner would notice. Live
planning is run with its prepared-graph cache warm, as on a warm serverless
instance. Reports hit rate, latency and quality per distance.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import statistics
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
os.environ.setdefault("RUNLINE_ALLOW_OSM_DOWNLOAD", "0")

from runline import planner, serving  # noqa: E402
from runline.coverage import areas, load_area_graph  # noqa: E402
from runline.geo import distance_meters  # noqa: E402
from runline.models import Coordinate, RoutePreferences, SurfacePreference  # noqa: E402

DEFAULT_DISTANCES = (3.0, 5.0, 6.5, 8.0, 12.0)


def summary(routes: list) -> dict:
    """Averages over a request's returned routes, per mile where that is fairer."""

    count = len(routes)
    return {
        "distance_error": sum(abs(r.metrics.distance_miles - r._target) for r in routes) / count,
        "signals_per_mile": sum(r.metrics.traffic_signal_events / r.metrics.distance_miles for r in routes) / count,
        "crossings_per_mile": sum(r.metrics.major_crossing_events / r.metrics.distance_miles for r in routes) / count,
        "repeated": sum(r.metrics.repeated_fraction for r in routes) / count,
        "compactness": sum(r.metrics.compactness for r in routes) / count,
    }


def plan_live(origin: Coordinate, preferences: RoutePreferences, cache: Path) -> tuple[list, float]:
    original = planner.serve_from_pool
    planner.serve_from_pool = lambda *_: None
    try:
        started = time.perf_counter()
        result = planner.plan_routes(origin, preferences, cache_directory=cache, elevation_source="none")
        return result.candidates, time.perf_counter() - started
    finally:
        planner.serve_from_pool = original


def main() -> int:
    parser = argparse.ArgumentParser(prog="compare_pools")
    parser.add_argument("--area", default="charlottesville-va")
    parser.add_argument("--starts", type=int, default=40)
    parser.add_argument("--radius-meters", type=float, default=6000)
    parser.add_argument("--distances", type=float, nargs="+", default=DEFAULT_DISTANCES)
    parser.add_argument("--surfaces", nargs="+", default=["mixed"], choices=[s.value for s in SurfacePreference])
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args()

    area = next(item for item in areas() if item.slug == arguments.area)
    if serving.pool_file(area) is None:
        print(f"no usable pool file for {area.slug} under {serving.pools_root()}")
        return 1
    graph = load_area_graph(area)
    near = [
        Coordinate(float(data["y"]), float(data["x"]))
        for _, data in graph.nodes(data=True)
        if distance_meters(area.center, Coordinate(float(data["y"]), float(data["x"]))) <= arguments.radius_meters
    ]
    starts = random.Random(arguments.seed).sample(near, arguments.starts)

    rows = []
    with tempfile.TemporaryDirectory() as directory:
        cache = Path(directory)
        for surface in arguments.surfaces:
            for miles in arguments.distances:
                for origin in starts:
                    preferences = RoutePreferences(miles, surface=SurfacePreference(surface))
                    began = time.perf_counter()
                    pooled = serving.serve_from_pool(origin, preferences)
                    pool_seconds = time.perf_counter() - began
                    live, live_seconds = plan_live(origin, preferences, cache)
                    row = {"surface": surface, "miles": miles, "hit": bool(pooled),
                           "pool_seconds": pool_seconds, "live_seconds": live_seconds}
                    for name, routes in (("pool", pooled or []), ("live", live)):
                        for route in routes:
                            route._target = miles
                        if routes:
                            row[name] = summary(routes)
                    rows.append(row)
                print(f"  {surface} {miles:g} mi done", flush=True)

    def median(values):
        values = list(values)
        return statistics.median(values) if values else float("nan")

    metrics = ("distance_error", "signals_per_mile", "crossings_per_mile", "repeated", "compactness")
    print(f"\n{len(starts)} starts within {arguments.radius_meters:.0f} m of the centre\n")
    print(f"{'mi':>5} {'hit':>6} {'pool ms':>8} {'live ms':>8}   "
          + "  ".join(f"{name.replace('_per_mile', '/mi'):>18}" for name in metrics) + "   (pool vs live, on hits)")
    for miles in arguments.distances:
        chosen = [row for row in rows if row["miles"] == miles]
        hits = [row for row in chosen if row["hit"] and "live" in row]
        cells = "  ".join(
            f"{median(r['pool'][m] for r in hits):>8.3f} vs {median(r['live'][m] for r in hits):>6.3f}" for m in metrics
        )
        print(f"{miles:>5g} {len(hits) / len(chosen):>6.0%} {median(r['pool_seconds'] for r in hits) * 1000:>8.0f} "
              f"{median(r['live_seconds'] for r in chosen) * 1000:>8.0f}   {cells}")
    if arguments.output:
        arguments.output.write_text(json.dumps(rows, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
