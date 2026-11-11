"""Merge precomputed pool shards into one SQLite file.

    python scripts/merge_pools.py --area charlottesville-va
    python scripts/merge_pools.py --plan /bigtemp/$USER/us-pools/plan.json.gz \\
        --input /bigtemp/$USER/us-pools

An area run reads pools/<area>/shard-*.jsonl.gz and writes
pools/<area>.sqlite; a metro run reads one shard per unit of the plan and
writes <input>/pools.sqlite, refusing to start while any unit is missing.
Either way there is one row per (cell, surface). Each row holds the pool
packed by runline.pools (format 2; read it with runline.pools.decode_pool), so
the file can be served as-is or uploaded to an object store. Shards built
from a different graph (area runs) or commit than the first one are refused
rather than mixed; a metro run records each tile's graph hash in ``tiles``,
each cell's owning tile in ``cells`` and the plan's metros in ``meta``, which
serving (runline.metros) needs. scripts/index_pool_tiles.py adds the last two
to a file merged before they existed.
"""

from __future__ import annotations

import argparse
import base64
import gzip
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from runline.pools import FORMAT_VERSION


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="merge_pools")
    parser.add_argument("--area")
    parser.add_argument("--plan", type=Path, help="plan.json.gz of a metro run")
    parser.add_argument("--input", type=Path, default=ROOT / "pools")
    parser.add_argument("--output", type=Path, help="defaults to <input>/<area>.sqlite or <input>/pools.sqlite")
    parser.add_argument("--expect-shards", type=int, help="fail unless this many shards exist")
    arguments = parser.parse_args(argv)
    if bool(arguments.area) == bool(arguments.plan):
        parser.error("pass exactly one of --area or --plan")

    extra = {}
    database_cells = []
    if arguments.plan:
        with gzip.open(arguments.plan, "rt", encoding="utf-8") as stream:
            plan = json.load(stream)
        units = [unit["id"] for unit in plan["units"]]
        extra = {"metros": plan.get("metros", [])}
        # Serving reads a cell's owning tile to load the graph its pool was
        # built on, or to plan live there when the cell has no usable pool.
        database_cells = [(cell, unit["tile"]) for unit in plan["units"] for cell in unit["cells"]]
        shards = [arguments.input / f"{unit}.jsonl.gz" for unit in units]
        missing = [shard.name for shard in shards if not shard.exists()]
        if missing:
            print(f"{len(missing)} of {len(shards)} units missing, e.g. {missing[:3]}; rerun the failed tasks")
            return 1
        # Tiles have their own graphs, so only the build must match.
        keys = ("format", "commit", "grid_meters", "targets")
        output = arguments.output or arguments.input / "pools.sqlite"
    else:
        shards = sorted((arguments.input / arguments.area).glob("shard-*.jsonl.gz"))
        if not shards:
            print(f"no shards under {arguments.input / arguments.area}")
            return 1
        if arguments.expect_shards and len(shards) != arguments.expect_shards:
            print(f"found {len(shards)} shards, expected {arguments.expect_shards}; rerun the failed tasks")
            return 1
        keys = ("format", "area", "graph", "commit", "grid_meters", "targets")
        output = arguments.output or arguments.input / f"{arguments.area}.sqlite"

    temporary = output.with_suffix(".tmp")
    temporary.unlink(missing_ok=True)
    database = sqlite3.connect(temporary)
    database.executescript(
        """
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE tiles (tile TEXT PRIMARY KEY, graph TEXT) WITHOUT ROWID;
        CREATE TABLE cells (cell TEXT PRIMARY KEY, tile TEXT NOT NULL) WITHOUT ROWID;
        CREATE TABLE pools (
            cell TEXT NOT NULL,
            surface TEXT NOT NULL,
            loops INTEGER NOT NULL,
            pool BLOB NOT NULL,
            PRIMARY KEY (cell, surface)
        ) WITHOUT ROWID;
        """
    )

    def refuse(message: str) -> int:
        print(message)
        database.close()
        temporary.unlink()
        return 1

    reference = None
    rows = 0
    for shard in shards:
        with gzip.open(shard, "rt", encoding="utf-8") as stream:
            header = json.loads(stream.readline())
            build = {key: header.get(key) for key in keys}
            if build["format"] != FORMAT_VERSION:
                return refuse(f"{shard.name} is pool format {build['format']}, expected {FORMAT_VERSION}; recompute it")
            if reference is None:
                reference = build
            elif build != reference:
                return refuse(f"{shard.name} was built from {build}, not {reference}")
            if "tile" in header:
                database.execute("INSERT OR REPLACE INTO tiles VALUES (?, ?)", (header["tile"], header["graph"]))
            for line in stream:
                record = json.loads(line)
                database.execute(
                    "INSERT OR REPLACE INTO pools VALUES (?, ?, ?, ?)",
                    (record["cell"], record["surface"], record["loops"], base64.b64decode(record["pool"])),
                )
                rows += 1
    database.executemany("INSERT OR REPLACE INTO cells VALUES (?, ?)", database_cells)
    database.executemany(
        "INSERT INTO meta VALUES (?, ?)",
        [(key, json.dumps(value)) for key, value in {**reference, **extra}.items()],
    )
    database.commit()
    database.close()
    temporary.replace(output)
    print(f"{output}: {rows} pools from {len(shards)} shards ({output.stat().st_size / 1048576:.1f}MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
