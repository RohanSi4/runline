"""Check a pool file, then check a running server answers from it.

    python scripts/pool_smoke.py --pool pools/charlottesville-va.sqlite
    python scripts/pool_smoke.py --pool pools/charlottesville-va.sqlite \\
        --api https://runline.example.com --cells 5

Without --api it only checks the file: pool format, and that it was built from
the graph in graphs/ (or RUNLINE_GRAPHS_DIR). Serving ignores a file that fails
either check and quietly plans live, so run this before copying a file up.

With --api it also posts real requests. Starts are read from the pool file at
run time, each the walkable node a sampled cell's loops start from, so no
coordinates live in this file. Each start is asked for every distance on its
pooled surface, and again on a surface its cell has no pool for, which must
plan live: the same starts measured both ways. Exits 1 if no request came back
``source: pool``, the handoff's definition of done for hosting pools.
"""

from __future__ import annotations

import argparse
import json
import random
import sqlite3
import statistics
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from runline.coverage import areas, graphs_root  # noqa: E402
from runline.pools import decode_pool  # noqa: E402
from runline.serving import _checked_pool_file  # noqa: E402

SURFACES = ("mixed", "road", "trail")


def check_file(path: Path) -> bool:
    slug = path.stem
    area = next((item for item in areas() if item.slug == slug), None)
    if area is None:
        print(f"{path.name}: no area {slug!r} in {graphs_root() / 'manifest.json'}; serving looks pools up by area slug")
        return False
    # The same check serving makes, so a pass here means serving will read it.
    checked = _checked_pool_file(str(path), str(graphs_root() / area.filename), path.stat().st_mtime)
    if checked is None:
        print(f"{path.name}: serving would ignore this file (see the warning above)")
        return False
    database = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        pools, loops = database.execute("SELECT COUNT(*), SUM(loops) FROM pools").fetchone()
    finally:
        database.close()
    print(f"{path.name}: ok, {pools} pools, {loops} loops, {path.stat().st_size / 1048576:.1f}MB, "
          f"matches {area.filename}")
    return True


def sample_starts(path: Path, count: int, seed: int) -> list[tuple[str, str, str, float, float]]:
    """(cell, pooled surface, unpooled surface or "", latitude, longitude) per sampled cell."""

    database = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        by_cell: dict[str, set[str]] = {}
        for cell, surface in database.execute("SELECT cell, surface FROM pools"):
            by_cell.setdefault(cell, set()).add(surface)
        chosen = random.Random(seed).sample(sorted(by_cell), min(count, len(by_cell)))
        starts = []
        for cell in chosen:
            pooled = "mixed" if "mixed" in by_cell[cell] else sorted(by_cell[cell])[0]
            missing = next((surface for surface in SURFACES if surface not in by_cell[cell]), "")
            (blob,) = database.execute(
                "SELECT pool FROM pools WHERE cell = ? AND surface = ?", (cell, pooled)
            ).fetchone()
            latitude, longitude = decode_pool(blob)["origin"]
            starts.append((cell, pooled, missing, latitude, longitude))
        return starts
    finally:
        database.close()


def post_route(api: str, latitude: float, longitude: float, miles: float, surface: str, timeout: float) -> tuple[str, float]:
    body = json.dumps(
        {"latitude": latitude, "longitude": longitude, "distance_miles": miles, "surface": surface}
    ).encode()
    request = urllib.request.Request(
        f"{api.rstrip('/')}/api/routes", data=body, headers={"Content-Type": "application/json"}
    )
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read())
    except urllib.error.HTTPError as error:
        return f"http {error.code}", time.perf_counter() - started
    return payload.get("source", "?"), time.perf_counter() - started


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="pool_smoke")
    parser.add_argument("--pool", type=Path, required=True, help="pools/<area>.sqlite")
    parser.add_argument("--api", help="base URL of a running server, e.g. http://127.0.0.1:8000")
    parser.add_argument("--cells", type=int, default=3, help="cells to sample from the pool file")
    parser.add_argument("--distances", type=float, nargs="+", default=(3.0, 8.0, 12.0))
    parser.add_argument("--no-live", action="store_true", help="skip the live comparison requests")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--timeout", type=float, default=120.0)
    arguments = parser.parse_args(argv)

    if not check_file(arguments.pool):
        return 1
    if not arguments.api:
        return 0

    times: dict[str, list[float]] = {}
    pooled_answers = 0
    for cell, pooled, missing, latitude, longitude in sample_starts(arguments.pool, arguments.cells, arguments.seed):
        for miles in arguments.distances:
            asks = [(pooled, "pool")]
            if missing and not arguments.no_live:
                asks.append((missing, "live"))
            for surface, expected in asks:
                source, seconds = post_route(arguments.api, latitude, longitude, miles, surface, arguments.timeout)
                times.setdefault(source, []).append(seconds)
                pooled_answers += source == "pool"
                # A pooled cell can still plan live when too few loops sit in
                # the distance band, so "live" where "pool" was hoped is a
                # miss, not a failure.
                note = "" if source == expected else f"  (expected {expected})"
                print(f"{cell} {surface:5} {miles:4.1f} mi: {source:6} {seconds * 1000:7.0f} ms{note}")

    for source, values in sorted(times.items()):
        print(f"{source}: {len(values)} requests, median {statistics.median(values) * 1000:.0f} ms, "
              f"max {max(values) * 1000:.0f} ms")
    if not pooled_answers:
        print("no request was answered from the pool file")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
