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
Each line holds one pool packed with runline.pools (format 2, base64).
Merge shards with scripts/merge_pools.py.
"""

from __future__ import annotations

import argparse
import base64
import gzip
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

from runline.compact import graph_file, read_graph
from runline.compact import graph_fingerprint as graph_hash
from runline.coverage import areas, load_area_graph
from runline.geo import distance_meters
# Re-exported: the grid moved to the package so tiling and serving share it.
from runline.grid import DEFAULT_GRID_METERS, cell_centre, cell_id
from runline.models import Coordinate, RoutePreferences, SurfacePreference
from runline.osm import (
    MAX_SHARED_LENGTH_FRACTION,
    PREPARED_KEY,
    _edge_lengths,
    _routable_core,
    collapse_graph,
    generate_loops,
)
from runline.pools import FORMAT_VERSION, encode_pool

# The planner homes in on its target, so a pool reliably holds loops near each
# target and only some in between. Measured on three Charlottesville cells,
# eight spread targets covered 2-13 of the 29 half-mile bands from 1 to 15
# miles; whole miles covered 3-17 at twice the time. Requests that miss the
# pool fall back to live generation.
# Capped at 12 miles: metro tiles (runline.tiling) are sized for a 12 mile
# loop, and longer requests are rare enough to generate live.
DEFAULT_TARGETS = tuple(range(1, 13))
# Ten per target keeps every distinct shape a request would plausibly see;
# twenty doubled storage for loops that rarely ranked into a top five.
POOL_SIZE_PER_TARGET = 10
# Neighbouring targets find many of the same loops with a block or two
# changed. Two loops this close in distance that share this much length are
# one loop as far as a runner is concerned, so only the first is kept.
DUPLICATE_DISTANCE_MILES = 0.25


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
    return {"format": FORMAT_VERSION, "commit": commit, "dirty_src": dirty}


def graph_fingerprint(area) -> str:
    """The hash serving checks pools against: the pickle's, or a compact twin's recorded one."""

    return graph_hash(graph_file(Path(os.environ.get("RUNLINE_GRAPHS_DIR", ROOT / "graphs")) / area.filename))


def drop_near_duplicates(core, candidates) -> list:
    """Candidates in order, minus any that repeat an earlier one.

    A repeat is within DUPLICATE_DISTANCE_MILES of a kept loop and shares at
    least MAX_SHARED_LENGTH_FRACTION of their combined length, the same test
    the planner uses within one target. The distance guard keeps loops that
    overlap heavily but serve different distance bands.
    """

    kept: list = []
    kept_edges: list[tuple[float, dict]] = []
    for candidate in candidates:
        distance = candidate.metrics.distance_miles
        edges = _edge_lengths(core, candidate.node_ids)
        total = sum(edges.values())
        duplicate = False
        for other_distance, other in kept_edges:
            if abs(other_distance - distance) > DUPLICATE_DISTANCE_MILES:
                continue
            shared = sum(min(length, other.get(edge, 0)) for edge, length in edges.items())
            union = total + sum(other.values()) - shared
            if union and shared / union >= MAX_SHARED_LENGTH_FRACTION:
                duplicate = True
                break
        if not duplicate:
            kept.append(candidate)
            kept_edges.append((distance, edges))
    return kept


CELL_NODES_KEY = "_runline_cell_nodes"


def cell_origin(core, cell: str) -> int | None:
    """The routable node in the cell nearest its centre, or None if it has none.

    Cells are listed from every mapped node, but loops start on the routable
    core. Taking the core node nearest the centre wherever it lies gave 377 of
    Charlottesville's 4,364 cells an origin outside the cell; 342 of those have
    no core node at all, and mostly got a copy of a neighbour's pool. Serving
    reads the neighbours' pools itself, so those cells now get none.
    """

    grid_meters = float(cell.split(":", 1)[0])
    cached = core.graph.get(CELL_NODES_KEY)
    if cached is None or cached[0] != grid_meters:
        by_cell: dict[str, list[int]] = {}
        for node, data in core.nodes(data=True):
            by_cell.setdefault(cell_id(float(data["y"]), float(data["x"]), grid_meters), []).append(node)
        cached = core.graph[CELL_NODES_KEY] = (grid_meters, by_cell)
    nodes = cached[1].get(cell)
    if not nodes:
        return None
    centre = cell_centre(cell)
    return min(
        nodes,
        key=lambda node: distance_meters(centre, Coordinate(float(core.nodes[node]["y"]), float(core.nodes[node]["x"]))),
    )


def cell_pool(core, cell: str, targets, surface: SurfacePreference) -> dict | None:
    origin_node = cell_origin(core, cell)
    if origin_node is None:
        return None
    data = core.nodes[origin_node]
    origin = Coordinate(float(data["y"]), float(data["x"]))
    candidates = {}
    for target in targets:
        preferences = RoutePreferences(target, surface=surface)
        for candidate in generate_loops(core, origin, preferences, pool_size=POOL_SIZE_PER_TARGET):
            candidates.setdefault(candidate.node_ids, candidate)
    if not candidates:
        return None
    loops = [
        {"nodes": list(candidate.node_ids), "metrics": asdict(candidate.metrics)}
        for candidate in drop_near_duplicates(core, list(candidates.values()))
    ]
    ordered = sorted(loops, key=lambda loop: loop["metrics"]["distance_miles"])
    return {
        "cell": cell,
        "surface": surface.value,
        "origin_node": origin_node,
        "origin": [origin.latitude, origin.longitude],
        "loops": ordered,
    }


def _read_cells(path: Path) -> list[str]:
    """Cell ids, one per line; blank lines and # comments are skipped."""

    lines = (line.split("#", 1)[0].strip() for line in path.read_text().splitlines())
    return [line for line in lines if line]


def run_shard(arguments) -> int:
    area = next((item for item in areas() if item.slug == arguments.area), None)
    if area is None:
        print(f"no shipped area {arguments.area!r}")
        return 1
    graph = load_area_graph(area)
    if not graph.graph.get("elevation_baked"):
        print("warning: graph has no baked elevation; run build_graphs.py --bake-only first")

    cells = area_cells(area, graph, arguments.grid_meters)
    if arguments.cells:
        wanted = set(_read_cells(arguments.cells))
        cells = [cell for cell in cells if cell in wanted]
        if len(cells) < len(wanted):
            # As for the whole area, a cell with no walkable node gets no pool.
            print(f"{len(wanted) - len(cells)} of {len(wanted)} listed cells have no walkable node in the area")
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
        "area": area.slug,
        "graph": graph_fingerprint(area),
        "grid_meters": arguments.grid_meters,
        "targets": list(arguments.targets),
        "surfaces": [surface.value for surface in surfaces],
        "shard": [arguments.shard_index, arguments.shard_count],
        **build_version(),
    }
    write_pools(output, header, graph, shard_cells, surfaces, arguments.targets)
    return 0


def write_pools(output: Path, header: dict, graph, cells: list[str], surfaces, targets) -> int:
    """Compute every cell's pools on ``graph`` and write them as one shard.

    Written to a temporary name and renamed, so a shard that exists is whole
    and a killed task leaves nothing to skip over.
    """

    temporary = output.with_suffix(".tmp")
    started = time.time()
    written = 0
    with gzip.open(temporary, "wt", encoding="utf-8") as stream:
        stream.write(json.dumps({"header": True, **header}) + "\n")
        for surface in surfaces:
            core = prepare_area(graph, surface)
            for position, cell in enumerate(cells, 1):
                pool = cell_pool(core, cell, targets, surface)
                if pool is not None:
                    record = {
                        "cell": cell,
                        "surface": surface.value,
                        "loops": len(pool["loops"]),
                        "pool": base64.b64encode(encode_pool(pool)).decode("ascii"),
                    }
                    stream.write(json.dumps(record, separators=(",", ":")) + "\n")
                    written += 1
                if position % 10 == 0:
                    elapsed = time.time() - started
                    print(f"  {surface.value}: {position}/{len(cells)} cells, {elapsed:.0f}s", flush=True)
    os.replace(temporary, output)
    print(f"{output.name}: {written} pools from {len(cells)} cells in {time.time() - started:.0f}s")
    return written


def task_units(units: list[dict], task_index: int, task_count: int) -> list[dict]:
    """A contiguous block of the plan's units, so one task mostly stays on one tile."""

    start = len(units) * task_index // task_count
    end = len(units) * (task_index + 1) // task_count
    return units[start:end]


def cells_with_nodes(graph, grid_meters: float) -> set[str]:
    return {
        cell_id(float(data["y"]), float(data["x"]), grid_meters) for _, data in graph.nodes(data=True)
    }


def run_plan(arguments) -> int:
    """Pools for a block of the units in a plan from plan_tile_pools.py.

    Each unit runs on its tile's own graph (built by build_tile_graphs.py), so
    searches stay tile-sized however big the metro is. Cells without a
    walkable node in the tile are skipped, as area runs skip them.
    """

    with gzip.open(arguments.plan, "rt", encoding="utf-8") as stream:
        plan = json.load(stream)
    units = task_units(plan["units"], arguments.shard_index, arguments.shard_count)
    if arguments.list:
        print(f"{len(plan['units'])} units; task {arguments.shard_index} of {arguments.shard_count} "
              f"takes {len(units)}")
        return 0
    if max(arguments.targets) > plan["max_miles"]:
        print(f"targets go to {max(arguments.targets)} mi but tiles are sized for {plan['max_miles']} mi")
        return 1

    arguments.output.mkdir(parents=True, exist_ok=True)
    surfaces = [SurfacePreference(value) for value in arguments.surfaces]

    def header(unit: dict, fingerprint: str | None) -> dict:
        return {
            "tile": unit["tile"],
            "unit": unit["id"],
            "graph": fingerprint,
            "grid_meters": plan["grid_meters"],
            "targets": list(arguments.targets),
            "surfaces": [surface.value for surface in surfaces],
            **build_version(),
        }

    loaded = None  # (tile, graph, fingerprint, cells with a node)
    for unit in units:
        output = arguments.output / f"{unit['id']}.jsonl.gz"
        if output.exists():
            print(f"{output.name} already written, skipping")
            continue
        if loaded is None or loaded[0] != unit["tile"]:
            path = graph_file(arguments.tile_graphs / f"{unit['tile']}.pkl.gz")
            if not path.exists():
                # build_tile_graphs writes no graph for an empty (all-water) tile.
                if (arguments.tile_graphs / f"{unit['tile']}.json").exists():
                    print(f"{unit['id']}: tile has no walkable ways")
                    write_pools(output, header(unit, None), None, [], [], [])
                    continue
                print(f"{unit['id']}: no graph at {path}")
                return 1
            graph = read_graph(path)
            if not graph.graph.get("elevation_baked"):
                print(f"warning: {unit['tile']} has no baked elevation")
            loaded = (
                unit["tile"],
                graph,
                graph_hash(path),
                cells_with_nodes(graph, plan["grid_meters"]),
            )
        tile, graph, fingerprint, walkable = loaded
        cells = [cell for cell in unit["cells"] if cell in walkable][: arguments.limit_cells]
        write_pools(output, header(unit, fingerprint), graph, cells, surfaces, arguments.targets)
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="precompute_pools")
    parser.add_argument("--area", help="area slug from graphs/manifest.json")
    parser.add_argument("--plan", type=Path, help="plan.json.gz from plan_tile_pools.py (metro tiles)")
    parser.add_argument("--tile-graphs", type=Path, help="tile graphs from build_tile_graphs.py, with --plan")
    parser.add_argument("--grid-meters", type=float, default=DEFAULT_GRID_METERS)
    parser.add_argument("--targets", type=float, nargs="+", default=DEFAULT_TARGETS)
    parser.add_argument(
        "--surfaces", nargs="+", default=[surface.value for surface in SurfacePreference],
        choices=[surface.value for surface in SurfacePreference],
    )
    # Several arrays can split one plan: TASK_OFFSET shifts each array's ids
    # (MaxArraySize is often 1001).
    parser.add_argument(
        "--shard-index",
        type=int,
        default=int(os.environ.get("TASK_OFFSET", 0)) + int(os.environ.get("SLURM_ARRAY_TASK_ID", 0)),
    )
    parser.add_argument("--shard-count", type=int, default=1)
    parser.add_argument("--output", type=Path, default=ROOT / "pools")
    parser.add_argument("--limit-cells", type=int, help="only the first N cells of the shard (testing)")
    # A local sample (e.g. compare_pools.py's starts) without the whole area;
    # shards then split just these cells.
    parser.add_argument("--cells", type=Path, help="area runs: only the cell ids listed in this file, one per line")
    parser.add_argument("--list", action="store_true", help="print the cell count and exit")
    arguments = parser.parse_args(argv)
    if not 0 <= arguments.shard_index < arguments.shard_count:
        parser.error("--shard-index must be in [0, --shard-count)")
    if arguments.plan:
        if not arguments.tile_graphs:
            parser.error("--plan needs --tile-graphs")
        return run_plan(arguments)
    if not arguments.area:
        parser.error("pass --area, or --plan with --tile-graphs")
    return run_shard(arguments)


if __name__ == "__main__":
    raise SystemExit(main())
