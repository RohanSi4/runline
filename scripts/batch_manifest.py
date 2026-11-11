"""Describe a finished metro batch, and check a copy of it against that description.

    python scripts/batch_manifest.py $BASE/batches/b1              # writes $BASE/batches/b1/manifest.json
    python scripts/batch_manifest.py --verify <copy>/batches/b1    # exit 1 on any mismatch
    python scripts/batch_manifest.py --list $BASE/batches/b1       # archived paths, relative to $BASE

A batch is laid out as slurm/run_batch.sh leaves it, relative to a base
directory ($BASE on the cluster, or wherever an archive was unpacked):

    batches/<batch>/pools/plan.json.gz     the plan (the only way back to the cell index)
    batches/<batch>/pools/<unit>.jsonl.gz  one shard per unit (they rebuild pools.sqlite)
    batches/<batch>/pools/pools.sqlite     the merged, indexed pools
    tile-graphs/<tile>.pkl.gz, .json       the exact graphs the pools were built on

The manifest lists each of those files with its size and sha256, and the
facts that make the batch servable: the plan's cells and tiles, each tile's
graph hash as recorded in pools.sqlite (what runline.metros reads) and as
computed from its file (what runline.serving computes), the pool cells,
pools and loops, the result of PRAGMA quick_check, and whether the cell index
is present. Writing refuses (exit 1, no manifest) a batch that is not
servable as it stands: a missing shard or tile graph, a tile graph whose hash
is not its pools', a failed quick_check or a missing index. --allow-problems
writes it anyway, with the problems listed, so a damaged batch can still be
archived before scratch loses it.

--verify rehashes every listed file of a copy, recomputes the facts, and
checks that every tile with pools has a graph whose hash matches. Files the
manifest does not list are ignored. --tile-graphs points at tile graphs kept
somewhere other than <base>/tile-graphs.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime
import gzip
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from runline.metros import METRO_INDEX_VERSION, POOLS_NAME, TILE_GRAPHS_NAME, _read_run  # noqa: E402
from runline.serving import file_sha256, graph_fingerprint  # noqa: E402

MANIFEST_VERSION = 1
MANIFEST_NAME = "manifest.json"
PLAN_NAME = "plan.json.gz"


class Layout:
    """Where a batch's files are, and the base-relative names the manifest uses for them."""

    def __init__(self, batch_dir: Path, tile_graphs: Path | None = None) -> None:
        self.batch_dir = batch_dir.resolve()
        self.name = self.batch_dir.name
        self.base = self.batch_dir.parent.parent
        self.tile_graphs = (tile_graphs or self.base / TILE_GRAPHS_NAME).resolve()
        self.pools_dir = self.batch_dir / "pools"
        self.prefix = f"batches/{self.name}"

    def pools_key(self, name: str) -> str:
        return f"{self.prefix}/pools/{name}"

    def graph_key(self, name: str) -> str:
        return f"{TILE_GRAPHS_NAME}/{name}"

    def path(self, key: str) -> Path:
        if key.startswith(f"{TILE_GRAPHS_NAME}/"):
            return self.tile_graphs / key.split("/", 1)[1]
        return self.base / key


def read_plan(path: Path) -> dict:
    with gzip.open(path, "rt", encoding="utf-8") as stream:
        return json.load(stream)


def expected_files(layout: Layout, plan: dict) -> tuple[list[str], list[str]]:
    """The batch's files (base-relative), and the problems from any that are missing."""

    keys = [layout.pools_key(PLAN_NAME), layout.pools_key(POOLS_NAME)]
    keys += [layout.pools_key(f"{unit['id']}.jsonl.gz") for unit in plan["units"]]
    missing = [key for key in keys if not layout.path(key).is_file()]
    for tile in sorted({unit["tile"] for unit in plan["units"]}):
        graph, stats = layout.graph_key(f"{tile}.pkl.gz"), layout.graph_key(f"{tile}.json")
        # An empty (all-water) tile has stats and no graph; whether a tile
        # needs a graph is decided from pools.sqlite in facts().
        keys += [key for key in (graph, stats) if layout.path(key).is_file()]
    present = [key for key in keys if key not in missing]
    return present, [f"missing {key}" for key in missing]


def hash_files(layout: Layout, keys: list[str], jobs: int) -> dict[str, dict]:
    """Size and sha256 of each file, hashed on ``jobs`` threads (hashlib releases the GIL)."""

    total = sum(layout.path(key).stat().st_size for key in keys)
    done, step, next_report = 0, max(total // 10, 1), max(total // 10, 1)
    records: dict[str, dict] = {}

    def one(key: str) -> tuple[str, dict]:
        path = layout.path(key)
        return key, {"size": path.stat().st_size, "sha256": file_sha256(path)}

    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
        for key, record in pool.map(one, keys):
            records[key] = record
            done += record["size"]
            if done >= next_report and total > 1 << 30:
                print(f"hashed {done / (1 << 30):.1f} of {total / (1 << 30):.1f} GB", file=sys.stderr, flush=True)
                next_report += step
    return records


def pool_facts(path: Path) -> tuple[dict, dict[str, str | None], set[str], list[str]]:
    """Facts of a merged pool file, each tile's recorded graph hash, and the tiles that have pools."""

    problems: list[str] = []
    database = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        check = [row[0] for row in database.execute("PRAGMA quick_check")]
        quick_check = "ok" if check == ["ok"] else "; ".join(check[:5])
        tables = {row[0] for row in database.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        meta = {key: json.loads(value) for key, value in database.execute("SELECT key, value FROM meta")}
        columns = {row[1] for row in database.execute("PRAGMA table_info(pools)")}
        pools, pool_cells, loops = database.execute(
            "SELECT COUNT(*), COUNT(DISTINCT cell), COALESCE(SUM(loops), 0) FROM pools"
        ).fetchone()
        indexed_cells = database.execute("SELECT COUNT(*) FROM cells").fetchone()[0] if "cells" in tables else None
        if "tile" in columns:
            pooled_tiles = {row[0] for row in database.execute("SELECT DISTINCT tile FROM pools")}
        elif "cells" in tables:
            pooled_tiles = {
                row[0] for row in database.execute("SELECT DISTINCT cells.tile FROM pools LEFT JOIN cells USING (cell)")
            }
        else:
            pooled_tiles = {None}
        raw_tiles = dict(database.execute("SELECT tile, graph FROM tiles"))
    finally:
        database.close()

    index = "cells" in tables and meta.get("metro_index") == METRO_INDEX_VERSION
    if index:
        # The tile hashes serving checks, read the way runline.metros reads them.
        run = _read_run(str(path), path.stat().st_mtime)
        recorded = {slug: tile.graph for slug, tile in run.tiles.items()} if run else raw_tiles
    else:
        recorded = raw_tiles
        problems.append(f"{path.name} has no cell index; run scripts/merge_pools.py --plan {PLAN_NAME} --add-index")
    if quick_check != "ok":
        problems.append(f"{path.name} failed PRAGMA quick_check: {quick_check}")
    if None in pooled_tiles:
        problems.append(f"{path.name} has pools with no tile")
        pooled_tiles.discard(None)
    facts = {
        "quick_check": quick_check,
        "cells_index": index,
        "format": meta.get("format"),
        "commit": meta.get("commit"),
        "dirty_src": meta.get("dirty_src"),
        "indexed_cells": indexed_cells,
        "pool_cells": pool_cells,
        "pools": pools,
        "loops": loops,
        "tiles": len(recorded),
        "pooled_tiles": len(pooled_tiles),
    }
    return facts, recorded, pooled_tiles, problems


def facts(layout: Layout, plan: dict, records: dict[str, dict]) -> tuple[dict, list[str]]:
    """What the batch holds, and why it could not be served as it is."""

    units = plan["units"]
    planned_tiles = sorted({unit["tile"] for unit in units})
    summary = {
        "plan": {
            "version": plan.get("version"),
            "metros": plan.get("metros", []),
            "grid_meters": plan["grid_meters"],
            "units": len(units),
            "cells": sum(len(unit["cells"]) for unit in units),
            "tiles": len(planned_tiles),
        },
        "shards": sum(1 for key in records if key.endswith(".jsonl.gz")),
    }
    problems: list[str] = []
    pools_key = layout.pools_key(POOLS_NAME)
    if pools_key not in records:
        return summary, problems
    try:
        pools, recorded, pooled, problems = pool_facts(layout.path(pools_key))
    except sqlite3.Error as error:
        return summary, [f"{POOLS_NAME} is unreadable: {error}"]
    summary["pools"] = pools
    if pools["cells_index"] and pools["indexed_cells"] != summary["plan"]["cells"]:
        problems.append(f"the cell index has {pools['indexed_cells']:,} cells and the plan {summary['plan']['cells']:,}")
    if set(recorded) != set(planned_tiles):
        problems.append(f"{POOLS_NAME} has {len(recorded):,} tiles and the plan {len(planned_tiles):,}; is this its plan?")

    tiles = {}
    for tile in planned_tiles:
        expected = recorded.get(tile)
        graph = records.get(layout.graph_key(f"{tile}.pkl.gz"))
        found = graph_fingerprint(graph["sha256"]) if graph else None
        tiles[tile] = {"graph": expected, "file": found, "pooled": tile in pooled}
        if expected is None:
            if tile in pooled:
                problems.append(f"{tile} has pools but no graph hash")
            elif layout.graph_key(f"{tile}.json") not in records:
                problems.append(f"{tile} has no graph and no stats file")
        elif found is None:
            problems.append(f"missing tile graph {layout.graph_key(f'{tile}.pkl.gz')} (pools need graph {expected})")
        elif found != expected:
            problems.append(f"tile graph {tile} has fingerprint {found}, its pools were built on {expected}")
    summary["tile_graphs"] = tiles
    return summary, problems


def git_commit() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=True
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def inspect(layout: Layout, jobs: int) -> tuple[dict, dict[str, dict], list[str]]:
    plan_path = layout.pools_dir / PLAN_NAME
    if not plan_path.is_file():
        return {}, {}, [f"no plan at {layout.pools_key(PLAN_NAME)}; rebuild it with scripts/plan_tile_pools.py"]
    plan = read_plan(plan_path)
    keys, problems = expected_files(layout, plan)
    records = hash_files(layout, keys, jobs)
    summary, more = facts(layout, plan, records)
    return summary, records, problems + more


def write(layout: Layout, output: Path, jobs: int, allow_problems: bool) -> int:
    summary, records, problems = inspect(layout, jobs)
    for problem in problems:
        print(f"problem: {problem}")
    if not records or (problems and not allow_problems):
        print(f"no manifest written: {len(problems)} problems (--allow-problems writes one anyway)")
        return 1
    manifest = {
        "manifest_version": MANIFEST_VERSION,
        "batch": layout.name,
        "created": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "tool_commit": git_commit(),
        "total_bytes": sum(record["size"] for record in records.values()),
        "files": [{"path": key, **records[key]} for key in records],
        **summary,
        "problems": problems,
    }
    temporary = output.with_suffix(".tmp")
    temporary.write_text(json.dumps(manifest, indent=1) + "\n", encoding="utf-8")
    temporary.replace(output)
    report(layout.name, manifest)
    print(f"wrote {output}")
    return 1 if problems else 0


def report(name: str, manifest: dict) -> None:
    plan, pools = manifest.get("plan", {}), manifest.get("pools", {})
    print(
        f"{name}: {len(manifest['files']):,} files, {manifest['total_bytes'] / (1 << 30):.2f} GB; "
        f"plan {plan.get('cells', 0):,} cells on {plan.get('tiles', 0):,} tiles; "
        f"{pools.get('pools', 0):,} pools in {pools.get('pool_cells', 0):,} cells, {pools.get('loops', 0):,} loops; "
        f"quick_check {pools.get('quick_check')}, cell index {'present' if pools.get('cells_index') else 'missing'}"
    )


def verify(layout: Layout, manifest_path: Path, jobs: int) -> int:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    problems: list[str] = []
    if manifest.get("manifest_version") != MANIFEST_VERSION:
        problems.append(f"manifest version {manifest.get('manifest_version')}, expected {MANIFEST_VERSION}")
    if manifest.get("batch") != layout.name:
        problems.append(f"manifest is for batch {manifest.get('batch')!r}, directory is {layout.name!r}")

    listed = {entry["path"]: entry for entry in manifest["files"]}
    present = []
    for key, entry in listed.items():
        path = layout.path(key)
        if not path.is_file():
            problems.append(f"missing {key}")
        else:
            # Hashed even at the wrong size, so a swapped tile graph also
            # shows as a fingerprint problem below.
            present.append(key)
            if path.stat().st_size != entry["size"]:
                problems.append(f"{key} is {path.stat().st_size:,} bytes, manifest says {entry['size']:,}")
    records = hash_files(layout, present, jobs)
    for key in present:
        if records[key]["size"] == listed[key]["size"] and records[key]["sha256"] != listed[key]["sha256"]:
            problems.append(f"{key} sha256 {records[key]['sha256'][:16]}..., manifest says {listed[key]['sha256'][:16]}...")

    plan_key = layout.pools_key(PLAN_NAME)
    if plan_key in records:
        # Facts from the copy as it is, including files that failed the checks above.
        summary, more = facts(layout, read_plan(layout.path(plan_key)), records)
        problems += [problem for problem in more if not problem.startswith("missing ")]
        for section in ("plan", "shards", "pools", "tile_graphs"):
            # A section the copy could not produce (an unreadable pools.sqlite) is already a problem.
            if section in manifest and section in summary and summary[section] != manifest[section]:
                problems.append(f"{section} differs from the manifest: {difference(summary.get(section), manifest[section])}")
    # Every pooled tile's graph against the hash its pools were built on, as
    # the manifest recorded it, even if the copy's pools.sqlite is unreadable.
    for tile, entry in manifest.get("tile_graphs", {}).items():
        record = records.get(layout.graph_key(f"{tile}.pkl.gz"))
        if entry["graph"] is None or record is None:
            continue
        found = graph_fingerprint(record["sha256"])
        if found != entry["graph"]:
            problems.append(f"tile graph {tile} has fingerprint {found}, its pools were built on {entry['graph']}")
    # A problem found two ways is reported once.
    problems = list(dict.fromkeys(problems))
    for problem in problems:
        print(f"problem: {problem}")
    if problems:
        print(f"{layout.name}: verify FAILED, {len(problems)} problems")
        return 1
    report(layout.name, manifest)
    print(f"{layout.name}: verified {len(listed):,} files against {manifest_path}")
    return 0


def difference(found, expected) -> str:
    if isinstance(found, dict) and isinstance(expected, dict):
        keys = [key for key in sorted(set(found) | set(expected)) if found.get(key) != expected.get(key)]
        return ", ".join(f"{key}: {found.get(key)!r} vs {expected.get(key)!r}" for key in keys[:5])
    return f"{found!r} vs {expected!r}"


def list_files(layout: Layout, manifest_path: Path) -> int:
    """The manifest itself first, then every file it lists, relative to the base."""

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    print(f"{layout.prefix}/{MANIFEST_NAME}")
    for entry in manifest["files"]:
        print(entry["path"])
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="batch_manifest")
    parser.add_argument("batch_dir", type=Path, help="<base>/batches/<batch>")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--verify", action="store_true", help="check this copy against its manifest")
    mode.add_argument("--list", action="store_true", help="print the manifest's paths, relative to <base>")
    parser.add_argument("--manifest", type=Path, help=f"default <batch_dir>/{MANIFEST_NAME}")
    parser.add_argument("--tile-graphs", type=Path, help=f"default <base>/{TILE_GRAPHS_NAME}")
    parser.add_argument(
        "--jobs", type=int, default=int(os.environ.get("SLURM_CPUS_PER_TASK", 2)), help="files hashed at once"
    )
    parser.add_argument("--allow-problems", action="store_true", help="write a manifest for a damaged batch")
    arguments = parser.parse_args(argv)

    if not arguments.batch_dir.is_dir():
        print(f"no batch directory {arguments.batch_dir}")
        return 1
    layout = Layout(arguments.batch_dir, arguments.tile_graphs)
    manifest = arguments.manifest or layout.batch_dir / MANIFEST_NAME
    if arguments.verify or arguments.list:
        if not manifest.is_file():
            print(f"no manifest at {manifest}")
            return 1
        return verify(layout, manifest, arguments.jobs) if arguments.verify else list_files(layout, manifest)
    return write(layout, manifest, arguments.jobs, arguments.allow_problems)


if __name__ == "__main__":
    raise SystemExit(main())
