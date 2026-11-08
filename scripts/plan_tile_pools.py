"""Split the metro precompute into work units for SLURM.

    python scripts/plan_tile_pools.py --output /bigtemp/$USER/us-pools
    python scripts/plan_tile_pools.py --batch denver-co austin-tx --output <dir>

Every cell of every metro in config/metros.json goes to exactly one tile
(runline.tiling.owned_cells), and each tile's cells are cut into units of at
most --cells-per-unit, sorted by tile so a task that takes a run of units
mostly keeps one tile graph loaded. Writes <output>/plan.json.gz, which
precompute_pools.py --plan and merge_pools.py --plan read.

A unit is the restart grain: it writes its own shard when done, and a
resubmitted array skips finished units. The DC pilot took about 70 s per
cell, so 50 cells is about an hour.

--metro plans those metros on their own, as a pilot does. --batch plans every
metro and keeps only the named metros' tiles, so batches run one after
another split the cells exactly as one run of all 50 would, with no cell in
two batches. slurm/run_batch.sh uses it with config/metro-batches.json.
"""

from __future__ import annotations

import argparse
import gzip
import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from shapely.geometry import shape  # noqa: E402

from runline.tiling import owned_cells  # noqa: E402

DEFAULT_CELLS_PER_UNIT = 50
PLAN_VERSION = 1


def plan_units(owned: dict[str, list[str]], cells_per_unit: int) -> list[dict]:
    units = []
    for tile in sorted(owned):
        cells = owned[tile]
        parts = math.ceil(len(cells) / cells_per_unit)
        for part in range(parts):
            units.append(
                {
                    "id": f"{tile}-p{part:02d}",
                    "tile": tile,
                    "cells": cells[part * cells_per_unit : (part + 1) * cells_per_unit],
                }
            )
    return units


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="plan_tile_pools")
    parser.add_argument("--metros", type=Path, default=ROOT / "config" / "metros.json")
    parser.add_argument("--boundaries", type=Path, default=ROOT / "config" / "metro-boundaries.geojson")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cells-per-unit", type=int, default=DEFAULT_CELLS_PER_UNIT)
    chosen = parser.add_mutually_exclusive_group()
    chosen.add_argument("--metro", nargs="+", help="only these metro slugs (a pilot)")
    chosen.add_argument("--batch", nargs="+", help="these metros' share of a plan of every metro")
    arguments = parser.parse_args(argv)

    config = json.loads(arguments.metros.read_text(encoding="utf-8"))
    if arguments.metro:
        config = {**config, "metros": [metro for metro in config["metros"] if metro["slug"] in arguments.metro]}
        if not config["metros"]:
            print(f"no metro matching {arguments.metro}")
            return 1
    boundaries = {
        feature["properties"]["slug"]: shape(feature["geometry"])
        for feature in json.loads(arguments.boundaries.read_text(encoding="utf-8"))["features"]
    }
    owned = owned_cells(config, boundaries)
    metros = [metro["slug"] for metro in config["metros"]]
    if arguments.batch:
        unknown = sorted(set(arguments.batch) - set(metros))
        if unknown:
            print(f"no metro matching {unknown}")
            return 1
        kept = {tile["slug"] for metro in config["metros"] if metro["slug"] in arguments.batch for tile in metro["tiles"]}
        owned = {tile: cells for tile, cells in owned.items() if tile in kept}
        metros = [slug for slug in metros if slug in arguments.batch]
    units = plan_units(owned, arguments.cells_per_unit)
    plan = {
        "version": PLAN_VERSION,
        "grid_meters": config["grid_meters"],
        "max_miles": config["max_miles"],
        "metros": metros,
        "units": units,
    }
    arguments.output.mkdir(parents=True, exist_ok=True)
    path = arguments.output / "plan.json.gz"
    with gzip.open(path, "wt", encoding="utf-8") as stream:
        json.dump(plan, stream, separators=(",", ":"))
    cells = sum(len(unit["cells"]) for unit in units)
    tiles = len({unit["tile"] for unit in units})
    print(f"{path}: {cells:,} cells on {tiles:,} tiles in {len(units):,} units")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
