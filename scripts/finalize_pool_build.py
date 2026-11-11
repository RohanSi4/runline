"""Watch the local build, audit/package it when complete, and record the result.

Keeps the Mac awake only while this job runs. A finished build with failed
shards gets one resumable retry at lower concurrency. The optional callback
receives the completed archive and audit JSON; it runs only after validation.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--graphs", type=Path, required=True)
    parser.add_argument("--pool-dir", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--deadline", required=True, help="ISO timestamp with timezone")
    parser.add_argument("--offline-demo", type=Path)
    parser.add_argument("--callback", type=Path)
    args = parser.parse_args(argv)
    deadline = dt.datetime.fromisoformat(args.deadline)
    if deadline.tzinfo is None:
        parser.error("deadline must include its timezone")
    state_path = args.output.parent / "completion-status.json"
    state = {"state": "waiting", "commit": args.commit, "retries": 0}

    def active_workers():
        # Read only this build's process metadata. Waiting for orphaned shard
        # workers prevents two resumptions from writing the same temp files.
        listing = subprocess.check_output(["ps", "-axo", "pid=,comm=,args="], text=True)
        marker = re.compile(r"--output " + re.escape(str(args.pool_dir.resolve())) + r"(?:\s|$)")
        return [line.split(None, 1)[0] for line in listing.splitlines()
                if ("build_local_pools.py" in line or "precompute_pools.py" in line) and marker.search(line)]

    def record(**changes):
        state.update(changes)
        state["updated_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
        state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = state_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(state, indent=2) + "\n")
        temporary.replace(state_path)
        print(json.dumps(state), flush=True)

    awake = None
    try:
        if sys.platform == "darwin":
            import os
            awake = subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())])
        pool = args.pool_dir / "charlottesville-va.sqlite"
        record()
        last_completed = -1
        while not pool.exists():
            if dt.datetime.now(dt.timezone.utc) >= deadline:
                raise TimeoutError("complete pool file was not ready before the artifact deadline")
            progress_path = args.pool_dir / "progress.json"
            if progress_path.exists():
                progress = json.loads(progress_path.read_text())
                count = len(progress["completed"])
                if count != last_completed:
                    record(completed=count, total=progress["shards"], failed=progress["failed"])
                    last_completed = count
                workers = active_workers()
                finished_failed = count + len(progress["failed"]) == progress["shards"] and progress["failed"]
                if not workers and (finished_failed or count < progress["shards"]):
                    if state["retries"]:
                        raise RuntimeError("shards still failed after the resumable retry")
                    record(state="retrying", retries=1)
                    command = [
                        sys.executable, str(ROOT / "scripts/build_local_pools.py"),
                        "--source-root", str(args.source_root), "--graphs", str(args.graphs),
                        "--output", str(args.pool_dir), "--workers", "3", "--shards", str(progress["shards"]),
                    ]
                    with (args.output.parent / "retry-build.log").open("w") as log:
                        subprocess.run(command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, check=True)
                    record(state="waiting")
            time.sleep(15)
        record(state="auditing-and-packaging")
        command = [
            sys.executable, str(ROOT / "scripts/package_presentation.py"),
            "--graphs", str(args.graphs), "--pools", str(pool),
            "--output", str(args.output), "--commit", args.commit,
        ]
        if args.offline_demo:
            command += ["--offline-demo", str(args.offline_demo)]
        with (args.output.parent / "complete-package.log").open("w") as log:
            subprocess.run(command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, check=True)
        record(state="packaged", artifact=str(args.output))
        if args.callback:
            record(state="uploading-draft-artifact")
            subprocess.run([
                sys.executable, str(args.callback), str(args.output), str(args.output) + ".json",
            ], cwd=ROOT, check=True)
        record(state="complete")
        return 0
    except Exception as error:
        record(state="failed", error=str(error))
        return 1
    finally:
        if awake is not None:
            awake.terminate()


if __name__ == "__main__":
    raise SystemExit(main())
