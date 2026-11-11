"""Add the cell-to-tile index serving needs to a metro pool file merged without it.

    python scripts/index_pool_tiles.py --plan /bigtemp/$USER/us-pools/plan.json.gz \\
        /bigtemp/$USER/us-pools/pools.sqlite

Serving (runline.metros) loads the graph of the tile that owns a request's
cell, so a metro file needs a ``cells`` table (cell -> tile) and the plan's
metros in ``meta``. scripts/merge_pools.py writes both now; files merged
before it did (the DC pilot, the test batch) get them here, from the plan the
run used. Every planned cell is indexed, so a cell with no pool can still be
planned live on its tile. Running it twice is safe.
"""

from __future__ import annotations

import argparse
import gzip
import json
import sqlite3
import sys
from pathlib import Path


def index(plan_path: Path, pool_path: Path) -> int:
    with gzip.open(plan_path, "rt", encoding="utf-8") as stream:
        plan = json.load(stream)
    owner = {cell: unit["tile"] for unit in plan["units"] for cell in unit["cells"]}

    database = sqlite3.connect(pool_path)
    try:
        meta = {key: json.loads(value) for key, value in database.execute("SELECT key, value FROM meta")}
        if "graph" in meta:
            print(f"{pool_path} is an area pool (one graph); it needs no tile index")
            return 1
        if float(meta["grid_meters"]) != float(plan["grid_meters"]):
            print(f"{pool_path} uses {meta['grid_meters']} m cells, the plan {plan['grid_meters']} m; wrong plan?")
            return 1
        cells = [cell for (cell,) in database.execute("SELECT DISTINCT cell FROM pools")]
        missing = [cell for cell in cells if cell not in owner]
        if missing:
            print(f"{len(missing)} of {len(cells)} pooled cells are not in the plan, e.g. {missing[:3]}; wrong plan?")
            return 1
        tiles = {tile for (tile,) in database.execute("SELECT tile FROM tiles")}
        unknown = {owner[cell] for cell in cells} - tiles
        if unknown:
            print(f"{len(unknown)} owning tiles have no graph hash in tiles, e.g. {sorted(unknown)[:3]}")
            return 1
        with database:
            database.execute("CREATE TABLE IF NOT EXISTS cells (cell TEXT PRIMARY KEY, tile TEXT NOT NULL) WITHOUT ROWID")
            database.executemany("INSERT OR REPLACE INTO cells VALUES (?, ?)", owner.items())
            database.execute(
                "INSERT OR REPLACE INTO meta VALUES ('metros', ?)", (json.dumps(plan.get("metros", [])),)
            )
    finally:
        database.close()
    print(f"{pool_path}: indexed {len(owner):,} cells ({len(cells):,} with pools) on {len(set(owner.values())):,} tiles for {plan.get('metros')}")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="index_pool_tiles")
    parser.add_argument("--plan", type=Path, required=True, help="plan.json.gz the run was made from")
    parser.add_argument("pools", type=Path, help="merged pools.sqlite of that run")
    arguments = parser.parse_args(argv)
    return index(arguments.plan, arguments.pools)


if __name__ == "__main__":
    sys.exit(main())
