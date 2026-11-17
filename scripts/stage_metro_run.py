"""Turn an archived metro batch into servable metro runs, one per metro, and smoke test them.

    python scripts/stage_metro_run.py <archive>/batches/b1 --metros-dir metros
    python scripts/stage_metro_run.py <archive>/batches/b1 --metros-dir metros --metro new-york-ny --link symlink

<archive> is where slurm/archive_batch.sh's parts were unpacked (the batch
layout in scripts/batch_manifest.py). Steps:

1. Verify the archive against its manifest (batch_manifest.py --verify);
   --skip-verify if that just ran.
2. For each metro of the batch (or each --metro), write
   <metros-dir>/<metro>/pools.sqlite holding that metro's tiles, cells and
   pools with the batch's meta and cell index, and
   <metro>/tile-graphs/ with its tile graphs (copied, or --link symlink or
   hardlink to the archive's). The pool file appears last, so a server
   reading <metros-dir> never sees half a run. An existing run is refused
   unless --replace, which moves it aside rather than deleting it.
3. Check each staged run: PRAGMA quick_check, the index runline.metros
   needs, every tile graph's hash against its pools, and the totals against
   the manifest.
4. Smoke: with only <metros-dir> as coverage (no shipped areas, no
   downloads, no remote API), request a loop at --smoke pooled cells per
   metro through the API and require ``source: pool``. Output names tiles
   and distances, never coordinates or cell ids.

Point RUNLINE_METROS_DIR at <metros-dir> to serve the result.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import batch_manifest  # noqa: E402
from runline.metros import POOLS_NAME, TILE_GRAPHS_NAME, _read_run  # noqa: E402
from runline.serving import file_sha256, graph_fingerprint  # noqa: E402

PART_SUFFIX = ".part"


def split_pools(source: Path, target: Path, metro: str, label: str) -> None:
    """Copy one metro's share of a batch's merged pools into a new file with the same schema."""

    target.unlink(missing_ok=True)
    database = sqlite3.connect(target.resolve().as_uri(), uri=True, isolation_level=None)
    try:
        database.execute("ATTACH DATABASE ? AS source", (source.resolve().as_uri() + "?mode=ro",))
        schema = database.execute(
            "SELECT type, sql FROM source.sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' "
            "ORDER BY type = 'table' DESC"
        ).fetchall()
        database.execute("BEGIN")
        for _, sql in schema:
            database.execute(sql)
        database.execute("INSERT INTO meta SELECT * FROM source.meta")
        database.execute("INSERT OR REPLACE INTO meta VALUES ('metros', ?)", (json.dumps({metro: label}),))
        database.execute("INSERT INTO tiles SELECT * FROM source.tiles WHERE metro = ?", (metro,))
        mine = "tile IN (SELECT tile FROM main.tiles)"
        database.execute(f"INSERT INTO cells SELECT * FROM source.cells WHERE {mine}")
        database.execute(f"INSERT INTO pools SELECT * FROM source.pools WHERE {mine}")
        database.execute("COMMIT")
        database.execute("DETACH DATABASE source")
    finally:
        database.close()


def place(source: Path, target: Path, link: str) -> None:
    if link == "symlink":
        target.symlink_to(source.resolve())
    elif link == "hardlink":
        os.link(source, target)
    else:
        shutil.copy2(source, target)


def stage_metro(layout, manifest: dict, metro: str, label: str, tiles: dict, directory: Path, link: str) -> Path:
    """Write one metro's run beside its final place, then move it in, pools file last."""

    final = directory / metro
    staging = directory / f"{metro}.staging"
    if staging.exists():
        shutil.rmtree(staging)
    graphs = staging / TILE_GRAPHS_NAME
    graphs.mkdir(parents=True)
    listed = {entry["path"] for entry in manifest["files"]}
    for tile in sorted(tiles):
        for name in (f"{tile}.pkl.gz", f"{tile}.json"):
            if layout.graph_key(name) in listed:
                place(layout.path(layout.graph_key(name)), graphs / name, link)
    split_pools(layout.pools_dir / POOLS_NAME, staging / (POOLS_NAME + PART_SUFFIX), metro, label)
    staging.rename(final)
    (final / (POOLS_NAME + PART_SUFFIX)).rename(final / POOLS_NAME)
    return final


def check_run(run: Path, manifest: dict) -> tuple[dict, list[str]]:
    """A staged run's pool facts, and why serving would not use all of it."""

    pools = run / POOLS_NAME
    facts, recorded, pooled, problems = batch_manifest.pool_facts(pools)
    if _read_run(str(pools), pools.stat().st_mtime) is None:
        problems.append(f"{run.name}: runline.metros does not accept {POOLS_NAME}")
    sha = {entry["path"]: entry["sha256"] for entry in manifest["files"]}
    for tile, expected in sorted(recorded.items()):
        if expected is None:
            if tile in pooled:
                problems.append(f"{run.name}: {tile} has pools but no graph hash")
            continue
        graph = run / TILE_GRAPHS_NAME / f"{tile}.pkl.gz"
        if not graph.exists():
            problems.append(f"{run.name}: missing tile graph {tile}")
            continue
        digest = file_sha256(graph)
        if graph_fingerprint(digest) != expected:
            problems.append(f"{run.name}: tile graph {tile} has fingerprint {graph_fingerprint(digest)}, pools need {expected}")
        if digest != sha.get(f"{TILE_GRAPHS_NAME}/{tile}.pkl.gz"):
            problems.append(f"{run.name}: tile graph {tile} is not the archived file")
    return facts, problems


def band(pool: dict) -> tuple[int, float]:
    """The distance with the most of the pool's loops within a quarter mile of it."""

    distances = [loop["metrics"]["distance_miles"] for loop in pool["loops"]]
    if not distances:
        return 0, 0.0
    return max((sum(abs(other - distance) <= 0.25 for other in distances), distance) for distance in distances)


def set_environment(values: dict[str, str | None]) -> None:
    for key, value in values.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def smoke(runs: list[Path], directory: Path, per_metro: int) -> list[str]:
    """Request loops at pooled cells through the API, with only ``directory`` as coverage."""

    scratch = Path(tempfile.mkdtemp(prefix="runline-stage-"))
    isolated = {
        "RUNLINE_METROS_DIR": str(directory.resolve()),
        "RUNLINE_GRAPHS_DIR": str(scratch / "no-graphs"),
        "RUNLINE_POOLS_DIR": str(scratch / "no-pools"),
        "RUNLINE_CACHE_DIR": str(scratch / "cache"),
        "RUNLINE_ALLOW_OSM_DOWNLOAD": "0",
        "RUNLINE_REMOTE_API": None,
    }
    saved = {key: os.environ.get(key) for key in isolated}
    set_environment(isolated)
    from runline import coverage

    try:
        coverage._read_manifest.cache_clear()
        return smoke_requests(runs, directory, per_metro)
    finally:
        set_environment(saved)
        coverage._read_manifest.cache_clear()
        shutil.rmtree(scratch, ignore_errors=True)


def smoke_requests(runs: list[Path], directory: Path, per_metro: int) -> list[str]:
    """Every request must come from a pool. Prints tiles and distances, never where."""

    from fastapi import HTTPException

    from runline import coverage, metros
    from runline.api import RouteRequest, routes
    from runline.models import Coordinate
    from runline.pools import decode_pool

    loaded = {run.pools.parent.resolve() for run in metros.metro_runs()}
    problems = []
    for run in runs:
        if run.resolve() not in loaded:
            problems.append(f"{run.name}: not loaded from {directory}")
            continue
        database = sqlite3.connect(f"file:{run / POOLS_NAME}?mode=ro", uri=True)
        try:
            # The fullest pool of each tile, fullest tiles first, so the
            # smoke spreads over tiles and asks where loops are plentiful.
            picked = database.execute(
                "SELECT tile, surface, pool, MAX(loops) AS most FROM pools GROUP BY tile ORDER BY most DESC, tile LIMIT ?",
                (per_metro,),
            ).fetchall()
        finally:
            database.close()
        if not picked:
            problems.append(f"{run.name}: no pools to smoke test")
        for index, (tile, surface, blob, _) in enumerate(picked, 1):
            pool = decode_pool(blob)
            miles = round(band(pool)[1], 2)
            origin = Coordinate(*pool["origin"])
            area = coverage.containing_area(origin)
            where = f"{run.name} smoke {index}: tile {tile}, {miles} mi {surface}"
            if area is None or Path(area.metro_pools or "").resolve() != (run / POOLS_NAME).resolve():
                problems.append(f"{where}: the start does not resolve to this run")
                continue
            started = time.perf_counter()
            try:
                payload = asyncio.run(routes(RouteRequest(
                    latitude=origin.latitude, longitude=origin.longitude,
                    distance_miles=miles, surface=surface, result_count=1,
                )))
            except HTTPException as error:
                problems.append(f"{where}: HTTP {error.status_code} {error.detail}")
                continue
            print(f"{where}: source {payload['source']}, {len(payload['routes'])} route(s) "
                  f"in {time.perf_counter() - started:.1f} s")
            if payload["source"] != "pool":
                problems.append(f"{where}: served from {payload['source']}, not a pool")
    return problems


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="stage_metro_run")
    parser.add_argument("batch_dir", type=Path, help="<archive>/batches/<batch>")
    parser.add_argument("--metros-dir", type=Path, required=True, help="what RUNLINE_METROS_DIR will point at")
    parser.add_argument("--metro", nargs="+", help="only these metros of the batch")
    parser.add_argument("--manifest", type=Path, help="default <batch_dir>/manifest.json")
    parser.add_argument("--tile-graphs", type=Path, help="default <archive>/tile-graphs")
    parser.add_argument("--link", choices=("copy", "symlink", "hardlink"), default="copy",
                        help="how tile graphs get into each run (default copy)")
    parser.add_argument("--replace", action="store_true", help="move an existing run aside and stage over it")
    parser.add_argument("--skip-verify", action="store_true", help="the archive was just verified")
    parser.add_argument("--smoke", type=int, default=3, help="pooled cells to request per metro (0 skips)")
    parser.add_argument("--jobs", type=int, default=2, help="files hashed at once while verifying")
    arguments = parser.parse_args(argv)

    layout = batch_manifest.Layout(arguments.batch_dir, arguments.tile_graphs)
    manifest_path = arguments.manifest or layout.batch_dir / batch_manifest.MANIFEST_NAME
    if not manifest_path.is_file():
        print(f"no manifest at {manifest_path}")
        return 1
    if not arguments.skip_verify and batch_manifest.verify(layout, manifest_path, arguments.jobs) != 0:
        print("not staging an archive that fails verification")
        return 1
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    database = sqlite3.connect(f"file:{layout.pools_dir / POOLS_NAME}?mode=ro", uri=True)
    try:
        labels = json.loads(database.execute("SELECT value FROM meta WHERE key = 'metros'").fetchone()[0])
        tiles: dict[str, dict] = {}
        for tile, metro in database.execute("SELECT tile, metro FROM tiles"):
            tiles.setdefault(metro, {})[tile] = True
    finally:
        database.close()
    chosen = arguments.metro or sorted(tiles)
    unknown = sorted(set(chosen) - set(tiles))
    if unknown:
        print(f"batch {layout.name} has no metro {unknown}; it has {sorted(tiles)}")
        return 1

    directory = arguments.metros_dir
    directory.mkdir(parents=True, exist_ok=True)
    for metro in chosen:
        final = directory / metro
        if final.exists():
            if not arguments.replace:
                print(f"{final} exists; pass --replace to move it aside")
                return 1
            stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
            final.rename(directory / f"{metro}.replaced-{stamp}")
            print(f"moved the old {metro} run to {metro}.replaced-{stamp}")

    runs, problems, totals = [], [], {"pools": 0, "pool_cells": 0, "loops": 0, "indexed_cells": 0}
    for metro in chosen:
        run = stage_metro(layout, manifest, metro, labels.get(metro, metro), tiles[metro], directory, arguments.link)
        facts, more = check_run(run, manifest)
        problems += more
        runs.append(run)
        for key in totals:
            totals[key] += facts[key] or 0
        print(f"staged {run}: {facts['tiles']:,} tiles, {facts['indexed_cells']:,} cells, "
              f"{facts['pools']:,} pools, {facts['loops']:,} loops, quick_check {facts['quick_check']}")
    if set(chosen) == set(tiles):
        expected = {key: manifest["pools"][key] for key in totals}
        if totals != expected:
            problems.append(f"staged runs total {totals}, the manifest {expected}")
    if not problems and arguments.smoke > 0:
        problems += smoke(runs, directory, arguments.smoke)
    for problem in problems:
        print(f"problem: {problem}")
    if problems:
        print(f"staging {layout.name} FAILED: {len(problems)} problems")
        return 1
    print(f"staged {len(runs)} metro run(s) of {layout.name}; serve with RUNLINE_METROS_DIR={directory}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
