"""Compare answers from precomputed pools with live planning on the same requests.

    python scripts/compare_pools.py --area charlottesville-va --starts 40

Starts are random walkable nodes near the area's centre (seeded, so runs are
comparable and no coordinates live in this file). Every request is answered
both ways and the top routes compared on what a runner would notice. Live
planning is run with its prepared-graph cache warm, as on a warm serverless
instance. Reports hit rate, latency and quality per distance, and why each
miss missed.

To test serving without the whole area's pools, write the cells serving can
read for these starts (each start's cell and its neighbours) and precompute
only those:

    python scripts/compare_pools.py --write-cells cells.txt
    python scripts/precompute_pools.py --area charlottesville-va --cells cells.txt --surfaces mixed
"""

from __future__ import annotations

import argparse
import collections
import json
import logging
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
from runline.grid import DEFAULT_GRID_METERS, cell_id, neighbouring_cells  # noqa: E402
from runline.models import Coordinate, RoutePreferences, SurfacePreference  # noqa: E402

DEFAULT_DISTANCES = (3.0, 5.0, 6.5, 8.0, 12.0)
METRICS = ("distance_error", "signals_per_mile", "crossings_per_mile", "repeated", "compactness")


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


class MissReasons(logging.Handler):
    """Collects the reason serving logs when it finds a pool but cannot use it."""

    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.reasons: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        reason = getattr(record, "pool_miss", None)
        if reason:
            self.reasons.append(reason)


def plan_live(origin: Coordinate, preferences: RoutePreferences, cache: Path) -> tuple[list, float]:
    original = planner.serve_from_pool
    planner.serve_from_pool = lambda *_: None
    try:
        started = time.perf_counter()
        result = planner.plan_routes(origin, preferences, cache_directory=cache, elevation_source="none")
        return result.candidates, time.perf_counter() - started
    finally:
        planner.serve_from_pool = original


def median(values) -> float:
    values = list(values)
    return statistics.median(values) if values else float("nan")


def comparison(rows: list[dict]) -> str:
    return "  ".join(
        f"{median(r['pool'][m] for r in rows if 'pool' in r):>8.3f} vs "
        f"{median(r['live'][m] for r in rows if 'live' in r):>6.3f}"
        for m in METRICS
    )


def main() -> int:
    parser = argparse.ArgumentParser(prog="compare_pools")
    parser.add_argument("--area", default="charlottesville-va")
    parser.add_argument("--starts", type=int, default=40)
    parser.add_argument("--radius-meters", type=float, default=6000)
    parser.add_argument("--distances", type=float, nargs="+", default=DEFAULT_DISTANCES)
    parser.add_argument("--surfaces", nargs="+", default=["mixed"], choices=[s.value for s in SurfacePreference])
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--write-cells", type=Path, help="write the cells serving reads for these starts and exit")
    parser.add_argument("--no-live", action="store_true", help="skip live planning: hit rate and pool metrics only")
    arguments = parser.parse_args()

    area = next(item for item in areas() if item.slug == arguments.area)
    if not arguments.write_cells and serving.pool_file(area) is None:
        print(f"no usable pool file for {area.slug} under {serving.pools_root()}")
        return 1
    graph = load_area_graph(area)
    near = [
        Coordinate(float(data["y"]), float(data["x"]))
        for _, data in graph.nodes(data=True)
        if distance_meters(area.center, Coordinate(float(data["y"]), float(data["x"]))) <= arguments.radius_meters
    ]
    starts = random.Random(arguments.seed).sample(near, arguments.starts)
    if arguments.write_cells:
        cells = sorted({
            neighbour
            for start in starts
            for neighbour in neighbouring_cells(cell_id(start.latitude, start.longitude, DEFAULT_GRID_METERS))
        })
        arguments.write_cells.write_text("".join(f"{cell}\n" for cell in cells))
        print(f"{len(cells)} cells written to {arguments.write_cells}")
        return 0

    misses = MissReasons()
    serving.LOGGER.addHandler(misses)
    serving.LOGGER.setLevel(logging.DEBUG)
    rows = []
    with tempfile.TemporaryDirectory() as directory:
        cache = Path(directory)
        for surface in arguments.surfaces:
            for miles in arguments.distances:
                for start_index, origin in enumerate(starts):
                    preferences = RoutePreferences(miles, surface=SurfacePreference(surface))
                    misses.reasons.clear()
                    began = time.perf_counter()
                    pooled = serving.serve_from_pool(origin, preferences)
                    pool_seconds = time.perf_counter() - began
                    if arguments.no_live:
                        live, live_seconds = [], float("nan")
                    else:
                        live, live_seconds = plan_live(origin, preferences, cache)
                    row = {"surface": surface, "miles": miles, "start": start_index, "hit": bool(pooled),
                           "returned": len(pooled or []), "pool_seconds": pool_seconds, "live_seconds": live_seconds}
                    if not pooled:
                        row["miss"] = misses.reasons[-1] if misses.reasons else "no pool for the cell"
                    for name, routes in (("pool", pooled or []), ("live", live)):
                        for route in routes:
                            route._target = miles
                        if routes:
                            row[name] = summary(routes)
                    rows.append(row)
                print(f"  {surface} {miles:g} mi done", flush=True)

    print(f"\n{len(starts)} starts within {arguments.radius_meters:.0f} m of the centre\n")
    print(f"{'mi':>5} {'hit':>6} {'pool ms':>8} {'live ms':>8}   "
          + "  ".join(f"{name.replace('_per_mile', '/mi'):>18}" for name in METRICS) + "   (pool vs live, on hits)")
    for miles in [*arguments.distances, None]:
        chosen = [row for row in rows if miles is None or row["miles"] == miles]
        hits = [row for row in chosen if row["hit"]]
        print(f"{'all' if miles is None else f'{miles:g}':>5} {len(hits) / len(chosen):>6.0%} "
              f"{median(r['pool_seconds'] for r in hits) * 1000:>8.0f} "
              f"{median(r['live_seconds'] for r in chosen) * 1000:>8.0f}   {comparison(hits)}")

    hits = [row for row in rows if row["hit"]]
    pool_ms = sorted(row["pool_seconds"] * 1000 for row in hits)
    if pool_ms:
        print(f"\npool ms on hits: p95 {pool_ms[int(0.95 * (len(pool_ms) - 1))]:.0f}, max {pool_ms[-1]:.0f}")
    short = [row for row in hits if row["returned"] < RoutePreferences(1).result_count]
    if short:
        print(f"{len(short)} hits returned fewer than the requested routes")
    missed = [row for row in rows if not row["hit"]]
    print(f"{len(missed)} misses of {len(rows)}:")
    for reason, count in collections.Counter(row["miss"] for row in missed).most_common():
        indices = sorted({row["start"] for row in missed if row["miss"] == reason})
        print(f"  {count:>3} {reason}, from {len(indices)} starts {indices}")
    if arguments.output:
        arguments.output.write_text(json.dumps(rows, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
