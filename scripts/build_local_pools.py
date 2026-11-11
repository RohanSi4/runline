"""Build a complete area's pools locally, with resumable shards and fixed sources.

Keep --source-root on a fixed commit while this runs. Each worker uses the
same graph and runtime; the merge rejects mixed builds. Nothing is uploaded.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--area", default="charlottesville-va")
    parser.add_argument("--source-root", type=Path, default=ROOT)
    parser.add_argument("--graphs", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=min(6, max(1, (os.cpu_count() or 2) - 2)))
    parser.add_argument("--shards", type=int, default=96)
    parser.add_argument("--limit-cells", type=int, help="smoke test only: cells per shard, not a full build")
    parser.add_argument("--cells", type=Path, help="sample only: a cell list from compare_pools.py --write-cells")
    parser.add_argument("--surfaces", nargs="+", choices=("road", "mixed", "trail"), default=["road", "mixed", "trail"])
    args = parser.parse_args(argv)
    if min(args.workers, args.shards) < 1:
        parser.error("workers and shards must be positive")
    if args.limit_cells is not None and args.limit_cells < 1:
        parser.error("limit-cells must be positive")
    source = args.source_root.resolve()
    graphs = args.graphs.resolve()
    output = args.output.resolve()
    if not (graphs / "manifest.json").is_file():
        parser.error("graph build has no manifest")
    if subprocess.run(["git", "diff", "--quiet", "HEAD", "--", "src"], cwd=source).returncode:
        parser.error("source tree has uncommitted routing changes")
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, text=True).strip()
    output.mkdir(parents=True, exist_ok=True)
    logs = output / "logs"
    logs.mkdir(exist_ok=True)
    environment = {
        **os.environ,
        "RUNLINE_GRAPHS_DIR": str(graphs),
        "RUNLINE_ALLOW_OSM_DOWNLOAD": "0",
        "OMP_NUM_THREADS": "1",
        "OPENBLAS_NUM_THREADS": "1",
        "MKL_NUM_THREADS": "1",
    }
    started = time.monotonic()
    report = {"area": args.area, "commit": commit, "shards": args.shards,
              "limit_cells": args.limit_cells, "cells": str(args.cells) if args.cells else None,
              "surfaces": args.surfaces, "completed": [], "failed": []}

    def worker(index: int) -> tuple[int, int]:
        # Catch a checkout change before starting another resumable shard.
        current = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, text=True).strip()
        if current != commit:
            return index, 1
        command = [
            sys.executable, str(source / "scripts/precompute_pools.py"),
            "--area", args.area, "--shard-index", str(index),
            "--shard-count", str(args.shards), "--output", str(output),
            "--surfaces", *args.surfaces,
        ]
        if args.limit_cells is not None:
            command += ["--limit-cells", str(args.limit_cells)]
        if args.cells is not None:
            command += ["--cells", str(args.cells.resolve())]
        with (logs / f"shard-{index:05d}.log").open("w") as log:
            result = subprocess.run(command, cwd=source, env=environment, stdout=log, stderr=subprocess.STDOUT)
        return index, result.returncode

    print(f"Building {args.area}: {args.shards} shards, {args.workers} workers, source {commit[:12]}", flush=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = [executor.submit(worker, index) for index in range(args.shards)]
        for future in concurrent.futures.as_completed(futures):
            index, code = future.result()
            report["failed" if code else "completed"].append(index)
            report["elapsed_seconds"] = round(time.monotonic() - started)
            temporary = output / "progress.tmp"
            temporary.write_text(json.dumps(report, indent=2) + "\n")
            temporary.replace(output / "progress.json")
            print(f"{len(report['completed'])}/{args.shards} complete, {len(report['failed'])} failed, "
                  f"{report['elapsed_seconds']}s elapsed (shard {index})", flush=True)
    if report["failed"]:
        print(f"Failed shards: {report['failed']}; see {logs}. Rerun to resume.", file=sys.stderr)
        return 1
    merged = output / f"{args.area}.sqlite"
    subprocess.run([
        sys.executable, str(source / "scripts/merge_pools.py"), "--area", args.area,
        "--input", str(output), "--output", str(merged), "--expect-shards", str(args.shards),
    ], cwd=source, env=environment, check=True)
    kind = "sample" if args.limit_cells is not None or args.cells is not None else "complete area"
    print(f"Merged {kind} pool file: {merged}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
