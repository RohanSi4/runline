"""Precompute route pools: loops of every length from a grid of start cells.

A pool is every distinct loop the planner finds from one start cell for one
surface preference, across a spread of targets. Distance and elevation
preference only affect ranking, so a request whose distance band the pool
covers can be served by filtering and ranking it, in milliseconds.

The work splits into shards so it can run as a SLURM array job:

    python scripts/precompute_pools.py --area charlottesville-va --list
    python scripts/precompute_pools.py --area charlottesville-va \\
        --shard-index 0 --shard-count 400

Each shard writes pools/<area>/shard-NNNNN.jsonl.gz and skips work it has
already written, so a requeued or failed task picks up where it stopped.
Merge shards with scripts/merge_pools.py.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import math
import os
import subprocess
import sys
import time
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
os.environ.setdefault("RUNLINE_ALLOW_OSM_DOWNLOAD", "0")

from runline.coverage import areas, load_area_graph
from runline.geo import distance_meters
# Re-exported: the grid moved to the package so tiling and serving share it.
from runline.grid import DEFAULT_GRID_METERS, cell_centre, cell_id
from runline.models import Coordinate, RoutePreferences, SurfacePreference
from runline.osm import (
    PREPARED_KEY,
    _nearest_node,
    _routable_core,
    collapse_graph,
    generate_loops,
)

# The planner homes in on its target, so a pool reliably holds loops near each
# target and only some in between. Measured on three Charlottesville cells,
# eight spread targets covered 2-13 of the 29 half-mile bands from 1 to 15
# miles; whole miles covered 3-17 at twice the time. Requests that miss the
# pool fall back to live generation.
DEFAULT_TARGETS = tuple(range(1, 16))
POOL_SIZE_PER_TARGET = 20
POOL_FORMAT_VERSION = 1


def area_cells(area, graph, grid_meters: float) -> list[str]:
    """Cells inside the area that have a mapped walkable node in them.

    A cell with no node is a field, a river or a rooftop; nobody starts a run
    there, and a request from one snaps to a neighbouring cell's node anyway.
    """

    cells = set()
    for _, data in graph.nodes(data=True):
        point = Coordinate(float(data["y"]), float(data["x"]))
        if distance_meters(area.center, point) <= area.radius_meters:
            cells.add(cell_id(point.latitude, point.longitude, grid_meters))
    return sorted(cells)


def prepare_area(graph, surface: SurfacePreference):
    """Collapse the whole area once per surface and reuse it for every cell.

    Live requests crop a disc around the start first, which is cheap per
    request but would be repeated for each of thousands of cells here.
    """

    core = _routable_core(collapse_graph(graph, RoutePreferences(1, surface=surface)))
    core.graph[PREPARED_KEY] = True
    return core


def build_version() -> dict:
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=True
        ).stdout.strip()
        dirty = bool(
            subprocess.run(
                ["git", "status", "--porcelain", "--", "src"], cwd=ROOT, capture_output=True, text=True
            ).stdout.strip()
        )
    except (OSError, subprocess.CalledProcessError):
        commit, dirty = "unknown", True
    return {"format": POOL_FORMAT_VERSION, "commit": commit, "dirty_src": dirty}


def graph_fingerprint(area) -> str:
    path = Path(os.environ.get("RUNLINE_GRAPHS_DIR", ROOT / "graphs")) / area.filename
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def cell_pool(core, cell: str, targets, surface: SurfacePreference) -> dict | None:
    origin_node = _nearest_node(core, cell_centre(cell))
    data = core.nodes[origin_node]
    origin = Coordinate(float(data["y"]), float(data["x"]))
    loops: dict[tuple[int, ...], dict] = {}
    for target in targets:
        preferences = RoutePreferences(target, surface=surface)
        for candidate in generate_loops(core, origin, preferences, pool_size=POOL_SIZE_PER_TARGET):
            if candidate.node_ids not in loops:
                loops[candidate.node_ids] = {
                    "nodes": list(candidate.node_ids),
                    "metrics": asdict(candidate.metrics),
                }
    if not loops:
        return None
    ordered = sorted(loops.values(), key=lambda loop: loop["metrics"]["distance_miles"])
    return {
        "cell": cell,
        "surface": surface.value,
        "origin_node": origin_node,
        "origin": [origin.latitude, origin.longitude],
        "loops": ordered,
    }


def run_shard(arguments) -> int:
    area = next((item for item in areas() if item.slug == arguments.area), None)
    if area is None:
        print(f"no shipped area {arguments.area!r}")
        return 1
    graph = load_area_graph(area)
    if not graph.graph.get("elevation_baked"):
        print("warning: graph has no baked elevation; run build_graphs.py --bake-only first")

    cells = area_cells(area, graph, arguments.grid_meters)
    if arguments.list:
        per_shard = math.ceil(len(cells) / arguments.shard_count)
        print(f"{area.slug}: {len(cells)} cells at {arguments.grid_meters:.0f}m, "
              f"{per_shard} per shard over {arguments.shard_count} shards")
        return 0

    shard_cells = cells[arguments.shard_index :: arguments.shard_count][: arguments.limit_cells]
    output_dir = arguments.output / area.slug
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / f"shard-{arguments.shard_index:05d}.jsonl.gz"
    if output.exists():
        print(f"{output.name} already written, skipping")
        return 0

    surfaces = [SurfacePreference(value) for value in arguments.surfaces]
    header = {
        "header": True,
        "area": area.slug,
        "graph": graph_fingerprint(area),
        "grid_meters": arguments.grid_meters,
        "targets": list(arguments.targets),
        "surfaces": [surface.value for surface in surfaces],
        "shard": [arguments.shard_index, arguments.shard_count],
        **build_version(),
    }
    temporary = output.with_suffix(".tmp")
    started = time.time()
    written = 0
    with gzip.open(temporary, "wt", encoding="utf-8") as stream:
        stream.write(json.dumps(header) + "\n")
        for surface in surfaces:
            core = prepare_area(graph, surface)
            for position, cell in enumerate(shard_cells, 1):
                pool = cell_pool(core, cell, arguments.targets, surface)
                if pool is not None:
                    stream.write(json.dumps(pool, separators=(",", ":")) + "\n")
                    written += 1
                if position % 10 == 0:
                    elapsed = time.time() - started
                    print(f"  {surface.value}: {position}/{len(shard_cells)} cells, {elapsed:.0f}s", flush=True)
    os.replace(temporary, output)
    print(f"{output.name}: {written} pools from {len(shard_cells)} cells in {time.time() - started:.0f}s")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="precompute_pools")
    parser.add_argument("--area", required=True, help="area slug from graphs/manifest.json")
    parser.add_argument("--grid-meters", type=float, default=DEFAULT_GRID_METERS)
    parser.add_argument("--targets", type=float, nargs="+", default=DEFAULT_TARGETS)
    parser.add_argument(
        "--surfaces", nargs="+", default=[surface.value for surface in SurfacePreference],
        choices=[surface.value for surface in SurfacePreference],
    )
    parser.add_argument(
        "--shard-index", type=int, default=int(os.environ.get("SLURM_ARRAY_TASK_ID", 0))
    )
    parser.add_argument("--shard-count", type=int, default=1)
    parser.add_argument("--output", type=Path, default=ROOT / "pools")
    parser.add_argument("--limit-cells", type=int, help="only the first N cells of the shard (testing)")
    parser.add_argument("--list", action="store_true", help="print the cell count and exit")
    arguments = parser.parse_args()
    if not 0 <= arguments.shard_index < arguments.shard_count:
        parser.error("--shard-index must be in [0, --shard-count)")
    return run_shard(arguments)


if __name__ == "__main__":
    raise SystemExit(main())
