"""Explain each pool miss on the compare_pools sample, and what would answer it.

    python scripts/pool_misses.py --surfaces mixed road trail --output output/pool-misses/diagnosis.json

Uses the same seeded starts as scripts/compare_pools.py (seed 7, 40 walkable
nodes within 6 km of the area centre) and the same requests. For every
request serving cannot answer, it walks serving's steps one at a time and
records the first one that leaves too few routes:

    no rows       no pool row for this surface in the cell or its 8 neighbours
    no band loops rows exist, but no stored loop is near the requested distance
    isolated      the runner's snapped node is off the graph's largest strongly
                  connected component, so it cannot reach the pooled loops
    join too far  in-band loops exist, but none passes within the connector
                  limit each way of the runner
    few in band   fewer than the minimum loops land in the band once joined
                  and measured as drawn
    doubling back, few distinct   as serving reports them

By default a miss is a request serve_from_pool, retries included, cannot
answer; --without-retries counts misses of the usual read alone (cell and 8
neighbours, 300 m connector, nearest node), which is what the retries were
chosen from. Either way the reasons describe the usual read.

It then reruns each miss with one change at a time (a wider ring of cells,
a longer connector, snapping to the connected core, one route accepted) to
show how many each would answer. Positions are reported only as cell offsets
and distance/bearing from the area centre, never as coordinates.

Read-only: it opens the pool file in SQLite read-only mode and writes only
--output.
"""

from __future__ import annotations

import argparse
import collections
import contextlib
import json
import math
import os
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
os.environ.setdefault("RUNLINE_ALLOW_OSM_DOWNLOAD", "0")

import networkx as nx  # noqa: E402

from runline import serving  # noqa: E402
from runline.coverage import areas, load_area_graph  # noqa: E402
from runline.geo import distance_meters  # noqa: E402
from runline.grid import cell_id, cell_row, neighbouring_cells  # noqa: E402
from runline.models import METERS_PER_MILE, Coordinate, RoutePreferences, SurfacePreference  # noqa: E402
from runline.osm import _nearest_node  # noqa: E402

DEFAULT_DISTANCES = (3.0, 5.0, 6.5, 8.0, 12.0)
OCTANTS = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")


def surfaces_by_cell(path: Path) -> dict[str, set[str]]:
    import sqlite3

    database = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        found: dict[str, set[str]] = {}
        for cell, surface in database.execute("SELECT cell, surface FROM pools"):
            found.setdefault(cell, set()).add(surface)
        return found
    finally:
        database.close()


def position(area, origin: Coordinate, grid_meters: float) -> dict:
    """Where a start is relative to the area centre: cell offsets, km and octant."""

    row = cell_row(origin.latitude, grid_meters)
    centre_row = cell_row(area.center.latitude, grid_meters)
    north = (origin.latitude - area.center.latitude) * 110_540.0
    east = (origin.longitude - area.center.longitude) * 111_320.0 * math.cos(math.radians(area.center.latitude))
    bearing = math.degrees(math.atan2(east, north)) % 360
    return {
        "row_offset": row - centre_row,
        "column_offset": round(east / grid_meters),
        "km": round(distance_meters(area.center, origin) / 1000, 1),
        "octant": OCTANTS[int((bearing + 22.5) // 45) % 8],
    }


@contextlib.contextmanager
def connector_limit(meters: float):
    original = serving.MAX_CONNECTOR_METERS
    serving.MAX_CONNECTOR_METERS = meters
    try:
        yield
    finally:
        serving.MAX_CONNECTOR_METERS = original


def attempt(graph, pool_source, origin: Coordinate, preferences: RoutePreferences, *, rings=1,
            connector=serving.MAX_CONNECTOR_METERS, minimum=None, start_node=None) -> dict:
    """serve_from_pool's steps with one knob changed, and counts at each step."""

    cell = cell_id(origin.latitude, origin.longitude, pool_source.grid_meters)
    surface = preferences.surface.value
    cells = neighbouring_cells(cell, rings)
    pools = serving.load_pools(pool_source, cells, surface)
    counts = {"pooled_cells": len(pools)}
    if not pools:
        return {**counts, "reason": "no rows", "routes": 0}
    target = preferences.target_distance_miles
    tolerance = preferences.distance_tolerance_miles + serving.BAND_SLACK_MILES
    longest = 2 * connector / METERS_PER_MILE
    band = [
        loop for pool in pools for loop in pool["loops"]
        if target - tolerance - longest <= loop["metrics"]["distance_miles"] <= target + tolerance
    ]
    counts["band_loops"] = len(band)
    with connector_limit(connector):
        node = start_node if start_node is not None else _nearest_node(graph, origin)
        reach = serving._reach(graph, node)
        counts["reachable"] = sum(serving._anchored(loop["nodes"], reach) is not None for loop in band)
        candidates = serving._candidates(pools, reach, preferences)
    counts["joined"] = len(candidates)
    if not counts["band_loops"]:
        return {**counts, "reason": "no band loops", "routes": 0}
    if not counts["reachable"]:
        return {**counts, "reason": "join too far", "routes": 0}
    if not candidates:
        # Loops pass within reach, but none lands in the band once the
        # connector is added.
        return {**counts, "reason": "few in band", "routes": 0}
    small = serving._route_graph(graph, [candidate.node_ids for candidate in candidates], preferences)
    measured = serving._measured(small, candidates, preferences)
    counts["measured"] = len(measured)
    counts["kept"] = sum(c.metrics.repeated_fraction <= serving.MAX_DOUBLED_BACK for c in measured)
    if minimum is None:
        ranked, reason = serving._ranked(small, measured, preferences)
    else:
        kept = [c for c in measured if c.metrics.repeated_fraction <= serving.MAX_DOUBLED_BACK]
        from runline.osm import _distinct_by_length
        from runline.scoring import rank_candidates

        ranked = _distinct_by_length(small, rank_candidates(kept, preferences), preferences.result_count)
        reason = "" if len(ranked) >= minimum else "few distinct"
    return {**counts, "reason": reason or "", "routes": len(ranked) if not reason else 0}


def nearest_join_meters(graph, node: int, pools: list[dict], preferences: RoutePreferences, cutoff=3000.0) -> float | None:
    """Shortest out-and-back to any in-band loop, searched past the connector limit."""

    target = preferences.target_distance_miles
    tolerance = preferences.distance_tolerance_miles + serving.BAND_SLACK_MILES
    loops = [
        loop for pool in pools for loop in pool["loops"]
        if target - tolerance - 2 * serving.MAX_CONNECTOR_METERS / METERS_PER_MILE
        <= loop["metrics"]["distance_miles"] <= target + tolerance
    ]
    out = nx.single_source_dijkstra_path_length(graph, node, cutoff=cutoff, weight="length")
    back = nx.single_source_dijkstra_path_length(graph.reverse(copy=False), node, cutoff=cutoff, weight="length")
    best = None
    for loop in loops:
        for loop_node in loop["nodes"]:
            if loop_node in out and loop_node in back:
                meters = out[loop_node] + back[loop_node]
                best = meters if best is None else min(best, meters)
    return best


def main() -> int:
    parser = argparse.ArgumentParser(prog="pool_misses")
    parser.add_argument("--area", default="charlottesville-va")
    parser.add_argument("--starts", type=int, default=40)
    parser.add_argument("--radius-meters", type=float, default=6000)
    parser.add_argument("--distances", type=float, nargs="+", default=DEFAULT_DISTANCES)
    parser.add_argument("--surfaces", nargs="+", default=["mixed", "road", "trail"],
                        choices=[s.value for s in SurfacePreference])
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--without-retries", action="store_true",
                        help="count hits by serving's usual read alone, as before its retries existed")
    arguments = parser.parse_args()

    area = next(item for item in areas() if item.slug == arguments.area)
    pool_source = serving.pool_file(area)
    if pool_source is None:
        print(f"no usable pool file for {area.slug} under {serving.pools_root()}")
        return 1
    graph = load_area_graph(area)
    # Same sample as compare_pools.py.
    near = [
        Coordinate(float(data["y"]), float(data["x"]))
        for _, data in graph.nodes(data=True)
        if distance_meters(area.center, Coordinate(float(data["y"]), float(data["x"]))) <= arguments.radius_meters
    ]
    starts = random.Random(arguments.seed).sample(near, arguments.starts)
    core = max(nx.strongly_connected_components(graph), key=len)
    pooled_surfaces = surfaces_by_cell(pool_source.path)

    rows = []
    for surface in arguments.surfaces:
        for miles in arguments.distances:
            for index, origin in enumerate(starts):
                preferences = RoutePreferences(miles, surface=SurfacePreference(surface))
                base = attempt(graph, pool_source, origin, preferences)
                hit = bool(base["routes"]) if arguments.without_retries else bool(
                    serving.serve_from_pool(origin, preferences))
                row = {"surface": surface, "miles": miles, "start": index, "hit": hit}
                if row["hit"]:
                    rows.append(row)
                    continue
                cell = cell_id(origin.latitude, origin.longitude, pool_source.grid_meters)
                node = _nearest_node(graph, origin)
                data = graph.nodes[node]
                reach = serving._reach(graph, node)
                ring = next((radius for radius in range(1, 11)
                             if any(surface in pooled_surfaces.get(c, ()) for c in neighbouring_cells(cell, radius))), None)
                reason = base["reason"]
                if reason in ("join too far", "no rows") and node not in core:
                    reason = "isolated"
                pools = serving.load_pools(pool_source, neighbouring_cells(cell), surface)
                row.update({
                    "reason": reason,
                    "serving_reason": base["reason"],
                    "counts": {key: value for key, value in base.items() if not key.startswith("_")},
                    "position": position(area, origin, pool_source.grid_meters),
                    "snap_meters": round(distance_meters(origin, Coordinate(float(data["y"]), float(data["x"]))), 1),
                    "on_core": node in core,
                    "reach_nodes": [len(reach.out_meters), len(reach.back_meters)],
                    "cell_surfaces": sorted(pooled_surfaces.get(cell, ())),
                    "nearest_pooled_ring": ring,
                    "nearest_join_meters": nearest_join_meters(graph, node, pools, preferences) if pools else None,
                })
                fixes = {
                    "ring 2": attempt(graph, pool_source, origin, preferences, rings=2),
                    "ring 3": attempt(graph, pool_source, origin, preferences, rings=3),
                    "connector 500": attempt(graph, pool_source, origin, preferences, connector=500.0),
                    "ring 2 + connector 500": attempt(graph, pool_source, origin, preferences, rings=2, connector=500.0),
                    "one route": attempt(graph, pool_source, origin, preferences, minimum=1),
                }
                snapped = serving._core_node(graph, node, origin)
                if snapped is not None:
                    fixes["snap to core"] = attempt(graph, pool_source, origin, preferences, start_node=snapped)
                    row["core_snap_meters"] = round(distance_meters(origin, Coordinate(
                        float(graph.nodes[snapped]["y"]), float(graph.nodes[snapped]["x"]))), 1)
                fixes["snap + ring 2 + connector 500"] = attempt(
                    graph, pool_source, origin, preferences, rings=2, connector=500.0, start_node=snapped)
                row["fixes"] = {name: result["routes"] for name, result in fixes.items()}
                rows.append(row)
            print(f"  {surface} {miles:g} mi done", flush=True)

    misses = [row for row in rows if not row["hit"]]
    print(f"\n{len(rows) - len(misses)} / {len(rows)} hits; {len(misses)} misses")
    print("\nby reason:")
    for reason, count in collections.Counter(row["reason"] for row in misses).most_common():
        starts_for = sorted({row["start"] for row in misses if row["reason"] == reason})
        print(f"  {count:>3} {reason:<16} starts {starts_for}")
    print("\nby surface:", dict(collections.Counter(row["surface"] for row in misses)))
    print("by distance:", dict(sorted(collections.Counter(row["miles"] for row in misses).items())))
    print("\nby start:")
    for index in sorted({row["start"] for row in misses}):
        mine = [row for row in misses if row["start"] == index]
        first = mine[0]
        print(f"  start {index:>2}: {len(mine):>2} misses, {dict(collections.Counter(r['reason'] for r in mine))}, "
              f"{first['position']}, snap {first['snap_meters']} m, on core {first['on_core']}, "
              f"reach {first['reach_nodes']}, cell surfaces {first['cell_surfaces']}, "
              f"pooled ring {first['nearest_pooled_ring']}, "
              f"surface x miles {[(r['surface'][0], r['miles']) for r in mine]}")
        for row in mine:
            print(f"      {row['surface']:<5} {row['miles']:>4g} {row['reason']:<14} {row['counts']} "
                  f"join {row['nearest_join_meters']} fixes {row['fixes']}")
    print("\nmisses each change alone would answer:")
    names = sorted({name for row in misses for name in row["fixes"]})
    for name in names:
        answered = [row for row in misses if row["fixes"].get(name)]
        print(f"  {name:<24} {len(answered):>3}  ({dict(collections.Counter(r['reason'] for r in answered))})")
    if arguments.output:
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        arguments.output.write_text(json.dumps(rows, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
