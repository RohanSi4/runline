"""Measure what serving a tile graph costs, to size the server.

    python scripts/measure_tile_graphs.py metros/washington-dc/tile-graphs
    python scripts/measure_tile_graphs.py <dir> --largest 8 --together 3
    python scripts/measure_tile_graphs.py graphs/charlottesville-va.pkl.gz

Each graph loads in a fresh process, the way runline.metros loads it on a
request's first use of a tile, and reports:

- hash: reading the file for the graph hash check (once per tile per process);
- load: unpickling it, and the resident memory it adds;
- index: the nearest-node arrays the first request on the tile builds;
- live: cropping and collapsing a 5 mile request around the tile's middle,
  what a pool miss pays before searching (runline.osm.prepare_core).

``--together K`` then loads the K largest into one process through the
serving cache, which shows whether memory adds up per tile. A server needs
about the process baseline plus RUNLINE_TILE_GRAPHS_CACHE times the largest
tile's load, plus a live request's working set, per worker.

With a directory, tiles are ranked by the node counts in build_tile_graphs.py's
<slug>.json stats (file size without them), and the largest are measured
along with the median one.
"""

from __future__ import annotations

import argparse
import gc
import json
import os
import resource
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

MEGABYTE = 1024 * 1024


def resident_mb() -> float:
    """Current resident memory, from /proc on Linux and ps elsewhere."""

    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) / 1024
    except OSError:
        pass
    output = subprocess.run(["ps", "-o", "rss=", "-p", str(os.getpid())], capture_output=True, text=True).stdout
    return int(output.strip()) / 1024


def peak_mb() -> float:
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # Bytes on macOS, kilobytes on Linux.
    return peak / MEGABYTE if sys.platform == "darwin" else peak / 1024


def measure_one(path: Path) -> dict:
    import numpy as np

    from runline.metros import read_graph_file
    from runline.models import Coordinate, RoutePreferences
    from runline.osm import _nearest_node, _routable_core, collapse_graph, crop_to_radius, graph_radius_meters
    from runline.serving import _fingerprint

    gc.collect()
    baseline = resident_mb()
    started = time.perf_counter()
    _fingerprint(path)
    hashed = time.perf_counter() - started

    started = time.perf_counter()
    graph = read_graph_file(path)
    loaded = time.perf_counter() - started
    gc.collect()
    after_load = resident_mb()

    latitudes = np.fromiter((data["y"] for _, data in graph.nodes(data=True)), float)
    longitudes = np.fromiter((data["x"] for _, data in graph.nodes(data=True)), float)
    middle = Coordinate(float(np.median(latitudes)), float(np.median(longitudes)))
    started = time.perf_counter()
    _nearest_node(graph, middle)
    indexed = time.perf_counter() - started

    preferences = RoutePreferences(5)
    started = time.perf_counter()
    core = _routable_core(collapse_graph(crop_to_radius(graph, middle, graph_radius_meters(preferences)), preferences))
    live = time.perf_counter() - started
    gc.collect()
    return {
        "file": str(path),
        "file_mb": round(path.stat().st_size / MEGABYTE, 1),
        "nodes": graph.number_of_nodes(),
        "edges": graph.number_of_edges(),
        "hash_s": round(hashed, 2),
        "load_s": round(loaded, 2),
        "index_s": round(indexed, 2),
        "live_s": round(live, 2),
        "live_nodes": core.number_of_nodes(),
        "baseline_mb": round(baseline),
        "graph_mb": round(after_load - baseline),
        "with_live_mb": round(resident_mb() - baseline),
        "peak_mb": round(peak_mb()),
    }


def measure_together(paths: list[Path]) -> dict:
    os.environ["RUNLINE_TILE_GRAPHS_CACHE"] = str(len(paths))
    from runline.metros import TILE_GRAPHS

    gc.collect()
    baseline = resident_mb()
    started = time.perf_counter()
    for path in paths:
        TILE_GRAPHS.get(path)
    gc.collect()
    return {
        "tiles": len(paths),
        "held": TILE_GRAPHS.held(),
        "load_s": round(time.perf_counter() - started, 1),
        "baseline_mb": round(baseline),
        "graphs_mb": round(resident_mb() - baseline),
        "peak_mb": round(peak_mb()),
    }


def nodes_from_stats(path: Path) -> int | None:
    stats = path.with_name(path.name.removesuffix(".pkl.gz") + ".json")
    if not stats.exists():
        return None
    return json.loads(stats.read_text()).get("nodes")


def chosen(arguments) -> list[Path]:
    paths = []
    for target in arguments.paths:
        paths.extend(sorted(target.glob("*.pkl.gz")) if target.is_dir() else [target])
    if len(paths) <= arguments.largest + 1:
        return paths
    ranked = sorted(paths, key=lambda path: nodes_from_stats(path) or path.stat().st_size, reverse=True)
    median = ranked[len(ranked) // 2]
    return ranked[: arguments.largest] + [median]


def child(*extra: str) -> dict:
    output = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), *extra], capture_output=True, text=True, check=True
    ).stdout
    return json.loads(output.strip().splitlines()[-1])


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="measure_tile_graphs")
    parser.add_argument("paths", type=Path, nargs="*", help="tile graph directories or .pkl.gz files")
    parser.add_argument("--largest", type=int, default=5, help="from a directory, measure this many largest tiles")
    parser.add_argument("--together", type=int, default=0, help="also load the K largest into one process")
    parser.add_argument("--one", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--all", type=Path, nargs="+", help=argparse.SUPPRESS)
    arguments = parser.parse_args(argv)
    if arguments.one:
        print(json.dumps(measure_one(arguments.one)))
        return 0
    if arguments.all:
        print(json.dumps(measure_together(arguments.all)))
        return 0
    if not arguments.paths:
        parser.error("pass a tile graph directory or files")

    paths = chosen(arguments)
    if not paths:
        print("no .pkl.gz graphs found")
        return 1
    print(f"{'tile':<34}{'file MB':>8}{'nodes':>10}{'hash s':>8}{'load s':>8}{'graph MB':>10}"
          f"{'index s':>9}{'live s':>8}{'+live MB':>9}{'peak MB':>9}")
    rows = []
    for path in paths:
        row = child("--one", str(path))
        rows.append(row)
        print(f"{Path(row['file']).name.removesuffix('.pkl.gz'):<34}{row['file_mb']:>8}{row['nodes']:>10,}"
              f"{row['hash_s']:>8}{row['load_s']:>8}{row['graph_mb']:>10}{row['index_s']:>9}"
              f"{row['live_s']:>8}{row['with_live_mb'] - row['graph_mb']:>9}{row['peak_mb']:>9}", flush=True)
    largest = max(rows, key=lambda row: row["graph_mb"])
    per_node = sum(row["graph_mb"] for row in rows) / max(1, sum(row["nodes"] for row in rows)) * 1000
    print(f"process baseline {rows[0]['baseline_mb']} MB; {per_node:.2f} MB per 1,000 nodes; "
          f"largest tile {largest['graph_mb']} MB loaded in {largest['load_s']} s")
    if arguments.together:
        group = sorted(paths, key=lambda path: nodes_from_stats(path) or path.stat().st_size, reverse=True)
        result = child("--all", *map(str, group[: arguments.together]))
        print(f"{result['tiles']} largest together: {result['graphs_mb']} MB resident over the baseline, "
              f"peak {result['peak_mb']} MB, {result['load_s']} s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
