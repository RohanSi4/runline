"""Merge precomputed pool shards into one SQLite file per area.

    python scripts/merge_pools.py --area charlottesville-va

Reads pools/<area>/shard-*.jsonl.gz and writes pools/<area>.sqlite with one
row per (cell, surface). Each row holds the gzipped JSON pool, so the file can
be served as-is or uploaded to an object store. Shards built from a different
graph or commit than the first one are refused rather than mixed.
"""

from __future__ import annotations

import argparse
import gzip
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(prog="merge_pools")
    parser.add_argument("--area", required=True)
    parser.add_argument("--input", type=Path, default=ROOT / "pools")
    parser.add_argument("--expect-shards", type=int, help="fail unless this many shards exist")
    arguments = parser.parse_args()

    shards = sorted((arguments.input / arguments.area).glob("shard-*.jsonl.gz"))
    if not shards:
        print(f"no shards under {arguments.input / arguments.area}")
        return 1
    if arguments.expect_shards and len(shards) != arguments.expect_shards:
        print(f"found {len(shards)} shards, expected {arguments.expect_shards}; rerun the failed tasks")
        return 1

    output = arguments.input / f"{arguments.area}.sqlite"
    temporary = output.with_suffix(".tmp")
    temporary.unlink(missing_ok=True)
    database = sqlite3.connect(temporary)
    database.executescript(
        """
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE pools (
            cell TEXT NOT NULL,
            surface TEXT NOT NULL,
            loops INTEGER NOT NULL,
            pool BLOB NOT NULL,
            PRIMARY KEY (cell, surface)
        ) WITHOUT ROWID;
        """
    )

    reference = None
    rows = 0
    for shard in shards:
        with gzip.open(shard, "rt", encoding="utf-8") as stream:
            header = json.loads(stream.readline())
            build = {key: header[key] for key in ("area", "graph", "commit", "grid_meters", "targets")}
            if reference is None:
                reference = build
            elif build != reference:
                print(f"{shard.name} was built from {build}, not {reference}")
                database.close()
                temporary.unlink()
                return 1
            for line in stream:
                pool = json.loads(line)
                database.execute(
                    "INSERT OR REPLACE INTO pools VALUES (?, ?, ?, ?)",
                    (
                        pool["cell"],
                        pool["surface"],
                        len(pool["loops"]),
                        gzip.compress(line.encode("utf-8"), 6),
                    ),
                )
                rows += 1
    database.executemany(
        "INSERT INTO meta VALUES (?, ?)",
        [(key, json.dumps(value)) for key, value in reference.items()],
    )
    database.commit()
    database.close()
    temporary.replace(output)
    print(f"{output}: {rows} pools from {len(shards)} shards ({output.stat().st_size / 1048576:.1f}MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
