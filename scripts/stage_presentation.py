"""Stage a separate Vercel preview with matching graph/pool artifacts.

The staging directory has its own API routing. The repository's production
Oracle proxy is preserved. A sample pool is an optimization: other covered
starts still plan live. --sample records that scope in the build manifest.
The staged vercel.json sets RUNLINE_BUILD_SHA to the staged source commit.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import shutil
import sqlite3
import subprocess
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class StageError(ValueError):
    """The supplied artifacts or checkout cannot be staged as a release."""


def fingerprint(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def stage(graphs: Path, pools: Path, output: Path, project_config: Path, *, sample: bool = False,
          offline_demo: Path | None = None, remote_api: str | None = None,
          verified_pools_sha256: str | None = None, root: Path = ROOT) -> dict:
    """Write a deployable directory and return its release report.

    A complete pool normally proves completeness with its build's progress.json.
    A pool whose SHA-256 matches ``verified_pools_sha256`` (a checked-in
    manifest of the audited complete file) is accepted without it.
    """
    if output.exists():
        raise StageError("choose a fresh staging directory")
    if subprocess.run(["git", "diff", "--quiet", "HEAD", "--", "src", "web"], cwd=root).returncode:
        raise StageError("commit application changes before staging a release")
    if remote_api and not remote_api.startswith("https://"):
        raise StageError("regional API must use HTTPS")
    manifest = json.loads((graphs / "manifest.json").read_text())
    area = next((a for a in manifest["areas"] if a["slug"] == pools.stem), None)
    if area is None:
        raise StageError("pool filename must identify an area in the supplied manifest")
    graph_hash = fingerprint(graphs / area["file"])
    with sqlite3.connect(f"file:{pools.resolve()}?mode=ro", uri=True) as database:
        meta = {key: json.loads(value) for key, value in database.execute("SELECT key, value FROM meta")}
        count, loops = database.execute("SELECT COUNT(*), SUM(loops) FROM pools").fetchone()
    if meta.get("format") != 2 or meta.get("graph") != graph_hash[:16] or not count:
        raise StageError("pool file is empty or does not match this graph and pool format")
    pools_hash = fingerprint(pools)
    if not sample and not (verified_pools_sha256 and pools_hash == verified_pools_sha256):
        progress_path = pools.parent / "progress.json"
        if not progress_path.exists():
            raise StageError("full build has no progress.json and no verified pool hash; use --sample for a sample preview")
        progress = json.loads(progress_path.read_text())
        if (progress.get("cells") or progress.get("limit_cells") or progress["failed"]
                or len(progress["completed"]) != progress["shards"]
                or set(progress["surfaces"]) != {"road", "mixed", "trail"}):
            raise StageError("full build is incomplete or filtered; use --sample for a sample preview")
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    source_changes = subprocess.check_output([
        "git", "diff", meta["commit"], commit, "--", "src/runline/osm.py", "src/runline/scoring.py",
        "src/runline/models.py", "scripts/precompute_pools.py",
    ], cwd=root, text=True)
    if source_changes:
        raise StageError("routing/generation changed since these pools were built; rebuild or review compatibility first")
    archive = subprocess.check_output(["git", "archive", commit, "app.py", "src", "web", "requirements.txt"], cwd=root)
    output.mkdir(parents=True)
    with tarfile.open(fileobj=io.BytesIO(archive)) as source:
        source.extractall(output, filter="data")
    destination = output / "graphs"
    destination.mkdir()
    for key in ("file", "drive_file", "starts_file", "places_file"):
        if area.get(key):
            shutil.copy2(graphs / area[key], destination / area[key])
    (destination / "manifest.json").write_text(json.dumps({"areas": [area]}, indent=2) + "\n")
    (output / "pools").mkdir()
    shutil.copy2(pools, output / "pools" / pools.name)
    config = json.loads((root / "vercel.json").read_text())
    config["routes"] = [{"src": "/(.*)", "dest": "app.py"}]
    config["builds"][0]["config"]["includeFiles"].append("pools/**")
    environment = config.setdefault("env", {})
    if remote_api:
        environment["RUNLINE_REMOTE_API"] = remote_api.rstrip("/")
    environment["RUNLINE_BUILD_SHA"] = commit
    (output / "vercel.json").write_text(json.dumps(config, indent=2) + "\n")
    (output / ".vercelignore").write_text(".git/\n.private/\n.env*\n**/__pycache__/\n")
    (output / ".vercel").mkdir()
    shutil.copy2(project_config, output / ".vercel/project.json")
    if offline_demo:
        shutil.copy2(offline_demo, output / "web/saved-demo.html")
    report = {
        "commit": commit, "pool_build_commit": meta["commit"], "graph": graph_hash,
        "pools": pools_hash, "pool_count": count, "loops": loops,
        "pool_scope": "sample" if sample else "complete area", "production_routing_changed": False,
    }
    (output / "release.json").write_text(json.dumps(report, indent=2) + "\n")
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--graphs", type=Path, required=True)
    parser.add_argument("--pools", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--project-config", type=Path, required=True, help="existing Vercel .vercel/project.json")
    parser.add_argument("--sample", action="store_true")
    parser.add_argument("--offline-demo", type=Path)
    parser.add_argument("--remote-api", help="regional API for starts outside the staged graph (use the backend's direct URL)")
    args = parser.parse_args(argv)
    try:
        report = stage(args.graphs, args.pools, args.output, args.project_config, sample=args.sample,
                       offline_demo=args.offline_demo, remote_api=args.remote_api)
    except StageError as error:
        parser.error(str(error))
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
