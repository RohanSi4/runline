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
from a different graph (area runs), commit, dirty tree, targets or surfaces
than the first one are refused rather than mixed; a metro run records each
tile's graph hash in ``tiles`` and refuses a tile whose units disagree on it.

A metro run also writes the index serving needs (runline.metros): ``cells``
maps every planned cell to the tile that owns it, ``pools.tile`` records the
tile each pool was built on (from its shard's header; the two must agree),
``tiles`` gains each tile's metro and disc, and ``meta`` gains the metro
labels and ``metro_index``. A
file merged before the index existed (the DC pilot) gets it from its plan
alone, without the shards:

    python scripts/merge_pools.py --plan plan.json.gz --add-index pools.sqlite
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

# Version of the cells/tiles/meta layout serving reads; runline.metros
# ignores a file whose index it does not know. Pool rows are unchanged.
METRO_INDEX_VERSION = 1
TILE_COLUMNS = (("metro", "TEXT"), ("latitude", "REAL"), ("longitude", "REAL"), ("radius_meters", "REAL"))


def plan_index(plan: dict, metros_path: Path) -> tuple[dict, dict]:
    """Each planned tile's metro and disc, and each metro's label.

    Read from the plan, or for a plan written before it listed them, from
    the metros config the plan was made from.
    """

    tiles, labels = plan.get("tiles"), plan.get("labels")
    if tiles is None or labels is None:
        from runline.tiling import metro_label

        config = json.loads(metros_path.read_text(encoding="utf-8"))
        tiles = {
            tile["slug"]: {
                "metro": metro["slug"],
                "latitude": tile["latitude"],
                "longitude": tile["longitude"],
                "radius_meters": tile["radius_meters"],
            }
            for metro in config["metros"]
            for tile in metro["tiles"]
        }
        labels = {metro["slug"]: metro_label(metro.get("name", metro["slug"])) for metro in config["metros"]}
    planned = {unit["tile"] for unit in plan["units"]}
    unknown = sorted(planned - set(tiles))
    if unknown:
        raise ValueError(
            f"{len(unknown)} planned tiles have no disc, e.g. {unknown[:3]}; pass the --metros it was planned from"
        )
    tiles = {slug: tiles[slug] for slug in sorted(planned)}
    labels = {slug: labels.get(slug, slug) for slug in sorted({tile["metro"] for tile in tiles.values()})}
    return tiles, labels


def write_index(database: sqlite3.Connection, plan: dict, tiles: dict, labels: dict) -> str | None:
    """Write the serving index into a merged metro file, or say why it does not fit."""

    present = {row[1] for row in database.execute("PRAGMA table_info(tiles)")}
    for column, kind in TILE_COLUMNS:
        if column not in present:
            database.execute(f"ALTER TABLE tiles ADD COLUMN {column} {kind}")
    merged = {row[0] for row in database.execute("SELECT tile FROM tiles")}
    if merged != set(tiles):
        return f"the file has {len(merged)} tiles and the plan {len(tiles)}; is this its plan?"
    database.executemany(
        "UPDATE tiles SET metro = ?, latitude = ?, longitude = ?, radius_meters = ? WHERE tile = ?",
        [(tile["metro"], tile["latitude"], tile["longitude"], tile["radius_meters"], slug) for slug, tile in tiles.items()],
    )
    # Every planned cell, not just those with pools: a start in a cell with
    # no pool still needs its tile's graph to plan live.
    database.execute("DROP TABLE IF EXISTS cells")
    database.execute("CREATE TABLE cells (cell TEXT PRIMARY KEY, tile TEXT NOT NULL) WITHOUT ROWID")
    database.executemany(
        "INSERT INTO cells VALUES (?, ?)",
        ((cell, unit["tile"]) for unit in plan["units"] for cell in unit["cells"]),
    )
    stray = database.execute("SELECT COUNT(*) FROM pools WHERE cell NOT IN (SELECT cell FROM cells)").fetchone()[0]
    if stray:
        return f"{stray} pools are for cells the plan does not have; is this its plan?"
    if "tile" not in {row[1] for row in database.execute("PRAGMA table_info(pools)")}:
        # Merged before pools recorded their tile: every unit's shard was
        # its planned tile's, so the plan says which. This rewrites every
        # row, so it takes minutes and its own size in free disk on DC.
        database.execute("ALTER TABLE pools ADD COLUMN tile TEXT")
        database.execute("UPDATE pools SET tile = (SELECT tile FROM cells WHERE cells.cell = pools.cell)")
    elsewhere = database.execute(
        "SELECT COUNT(*) FROM pools JOIN cells USING (cell) WHERE pools.tile IS NOT cells.tile"
    ).fetchone()[0]
    if elsewhere:
        return f"{elsewhere} pools were built on a tile the plan does not give their cell; is this its plan?"
    grid = database.execute("SELECT value FROM meta WHERE key = 'grid_meters'").fetchone()
    if grid is None or json.loads(grid[0]) != plan["grid_meters"]:
        return "the file and the plan use different grids"
    database.executemany(
        "INSERT OR REPLACE INTO meta VALUES (?, ?)",
        [("metros", json.dumps(labels)), ("metro_index", json.dumps(METRO_INDEX_VERSION))],
    )
    return None


def add_index(path: Path, plan: dict, metros_path: Path) -> int:
    if not path.exists():
        print(f"no merged file at {path}")
        return 1
    try:
        tiles, labels = plan_index(plan, metros_path)
    except ValueError as error:
        print(error)
        return 1
    # One explicit transaction: sqlite3 would otherwise commit the ALTER and
    # DROP at once, and a refused plan would leave the file half indexed.
    database = sqlite3.connect(path, isolation_level=None)
    try:
        database.execute("BEGIN")
        problem = write_index(database, plan, tiles, labels)
        if problem:
            database.execute("ROLLBACK")
            print(f"{path}: {problem}")
            return 1
        database.execute("COMMIT")
        cells = database.execute("SELECT COUNT(*) FROM cells").fetchone()[0]
    finally:
        database.close()
    print(f"{path}: indexed {cells:,} cells on {len(tiles):,} tiles of {', '.join(labels.values())}")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="merge_pools")
    parser.add_argument("--area")
    parser.add_argument("--plan", type=Path, help="plan.json.gz of a metro run")
    parser.add_argument("--input", type=Path, default=ROOT / "pools")
    parser.add_argument("--output", type=Path, help="defaults to <input>/<area>.sqlite or <input>/pools.sqlite")
    parser.add_argument("--expect-shards", type=int, help="fail unless this many shards exist")
    parser.add_argument("--add-index", type=Path, help="with --plan: index this merged file instead of merging")
    parser.add_argument(
        "--metros", type=Path, default=ROOT / "config" / "metros.json",
        help="tile discs and labels, for a plan written before it listed them",
    )
    arguments = parser.parse_args(argv)
    if bool(arguments.area) == bool(arguments.plan):
        parser.error("pass exactly one of --area or --plan")
    if arguments.add_index and not arguments.plan:
        parser.error("--add-index needs --plan")

    plan = index = None
    if arguments.plan:
        with gzip.open(arguments.plan, "rt", encoding="utf-8") as stream:
            plan = json.load(stream)
        if arguments.add_index:
            return add_index(arguments.add_index, plan, arguments.metros)
        try:
            index = plan_index(plan, arguments.metros)
        except ValueError as error:
            print(error)
            return 1
        shards = [arguments.input / f"{unit['id']}.jsonl.gz" for unit in plan["units"]]
        missing = [shard.name for shard in shards if not shard.exists()]
        if missing:
            print(f"{len(missing)} of {len(shards)} units missing, e.g. {missing[:3]}; rerun the failed tasks")
            return 1
        # Tiles have their own graphs, so only the build must match; each
        # tile's graph is checked across its units below.
        keys = ("format", "commit", "dirty_src", "grid_meters", "targets", "surfaces")
        output = arguments.output or arguments.input / "pools.sqlite"
    else:
        shards = sorted((arguments.input / arguments.area).glob("shard-*.jsonl.gz"))
        if not shards:
            print(f"no shards under {arguments.input / arguments.area}")
            return 1
        if arguments.expect_shards and len(shards) != arguments.expect_shards:
            print(f"found {len(shards)} shards, expected {arguments.expect_shards}; rerun the failed tasks")
            return 1
        keys = ("format", "area", "graph", "commit", "dirty_src", "grid_meters", "targets", "surfaces")
        output = arguments.output or arguments.input / f"{arguments.area}.sqlite"

    temporary = output.with_suffix(".tmp")
    temporary.unlink(missing_ok=True)
    database = sqlite3.connect(temporary)
    database.executescript(
        """
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE tiles (tile TEXT PRIMARY KEY, graph TEXT) WITHOUT ROWID;
        CREATE TABLE pools (
            cell TEXT NOT NULL,
            surface TEXT NOT NULL,
            loops INTEGER NOT NULL,
            pool BLOB NOT NULL,
            tile TEXT,
            PRIMARY KEY (cell, surface)
        ) WITHOUT ROWID;
        """
    )
    # tile: the tile whose graph a metro pool's node ids belong to, from its
    # shard's header; NULL for an area run.

    def refuse(message: str) -> int:
        print(message)
        database.close()
        temporary.unlink()
        return 1

    reference = None
    tile_graphs: dict[str, str | None] = {}
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
            tile = header.get("tile")
            if tile is not None:
                # Units of one tile on different graphs would serve some
                # cells' node ids on the wrong graph.
                if tile_graphs.setdefault(tile, header["graph"]) != header["graph"]:
                    return refuse(
                        f"{shard.name} was built on {tile} graph {header['graph']}, "
                        f"other units on {tile_graphs[tile]}; recompute the stale units"
                    )
                database.execute("INSERT OR REPLACE INTO tiles VALUES (?, ?)", (tile, header["graph"]))
            for line in stream:
                record = json.loads(line)
                database.execute(
                    "INSERT OR REPLACE INTO pools VALUES (?, ?, ?, ?, ?)",
                    (record["cell"], record["surface"], record["loops"], base64.b64decode(record["pool"]), tile),
                )
                rows += 1
    database.executemany(
        "INSERT INTO meta VALUES (?, ?)",
        [(key, json.dumps(value)) for key, value in reference.items()],
    )
    if plan is not None:
        problem = write_index(database, plan, *index)
        if problem:
            return refuse(problem)
    database.commit()
    database.close()
    temporary.replace(output)
    print(f"{output}: {rows} pools from {len(shards)} shards ({output.stat().st_size / 1048576:.1f}MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
