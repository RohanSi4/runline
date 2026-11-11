"""Stage a separate Vercel preview with matching graph/pool artifacts.

The staging directory has its own API routing. The repository's production
Oracle proxy is preserved. A sample pool is an optimization: other covered
starts still plan live. --sample records that scope in the build manifest.
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


def fingerprint(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--graphs", type=Path, required=True)
    parser.add_argument("--pools", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--project-config", type=Path, required=True, help="existing Vercel .vercel/project.json")
    parser.add_argument("--sample", action="store_true")
    parser.add_argument("--offline-demo", type=Path)
    args = parser.parse_args(argv)
    if args.output.exists():
        parser.error("choose a fresh staging directory")
    if subprocess.run(["git", "diff", "--quiet", "HEAD", "--", "src", "web"], cwd=ROOT).returncode:
        parser.error("commit application changes before staging a release")
    manifest = json.loads((args.graphs / "manifest.json").read_text())
    area = next((a for a in manifest["areas"] if a["slug"] == args.pools.stem), None)
    if area is None:
        parser.error("pool filename must identify an area in the supplied manifest")
    graph_hash = fingerprint(args.graphs / area["file"])
    with sqlite3.connect(f"file:{args.pools.resolve()}?mode=ro", uri=True) as database:
        meta = {key: json.loads(value) for key, value in database.execute("SELECT key, value FROM meta")}
        count, loops = database.execute("SELECT COUNT(*), SUM(loops) FROM pools").fetchone()
    if meta.get("format") != 2 or meta.get("graph") != graph_hash[:16] or not count:
        parser.error("pool file is empty or does not match this graph and pool format")
    if not args.sample:
        progress = json.loads((args.pools.parent / "progress.json").read_text())
        if (progress.get("cells") or progress.get("limit_cells") or progress["failed"]
                or len(progress["completed"]) != progress["shards"]
                or set(progress["surfaces"]) != {"road", "mixed", "trail"}):
            parser.error("full build is incomplete or filtered; use --sample for a sample preview")
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    source_changes = subprocess.check_output([
        "git", "diff", meta["commit"], commit, "--", "src/runline/osm.py", "src/runline/scoring.py",
        "src/runline/models.py", "scripts/precompute_pools.py",
    ], cwd=ROOT, text=True)
    if source_changes:
        parser.error("routing/generation changed since these pools were built; rebuild or review compatibility first")
    archive = subprocess.check_output(["git", "archive", commit, "app.py", "src", "web", "requirements.txt"], cwd=ROOT)
    args.output.mkdir(parents=True)
    with tarfile.open(fileobj=io.BytesIO(archive)) as source:
        source.extractall(args.output, filter="data")
    destination = args.output / "graphs"
    destination.mkdir()
    for key in ("file", "drive_file", "starts_file", "places_file"):
        if area.get(key):
            shutil.copy2(args.graphs / area[key], destination / area[key])
    (destination / "manifest.json").write_text(json.dumps({"areas": [area]}, indent=2) + "\n")
    (args.output / "pools").mkdir()
    shutil.copy2(args.pools, args.output / "pools" / args.pools.name)
    config = json.loads((ROOT / "vercel.json").read_text())
    config["routes"] = [{"src": "/(.*)", "dest": "app.py"}]
    config["builds"][0]["config"]["includeFiles"].append("pools/**")
    (args.output / "vercel.json").write_text(json.dumps(config, indent=2) + "\n")
    (args.output / ".vercelignore").write_text(".git/\n**/__pycache__/\n")
    (args.output / ".vercel").mkdir()
    shutil.copy2(args.project_config, args.output / ".vercel/project.json")
    if args.offline_demo:
        shutil.copy2(args.offline_demo, args.output / "web/saved-demo.html")
    report = {
        "commit": commit, "pool_build_commit": meta["commit"], "graph": graph_hash,
        "pools": fingerprint(args.pools), "pool_count": count, "loops": loops,
        "pool_scope": "sample" if args.sample else "complete area", "production_routing_changed": False,
    }
    (args.output / "release.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
