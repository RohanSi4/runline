"""Audit and package an exact application/graph/pool snapshot for handoff.

Generated coordinates remain in gitignored artifacts. --sample explicitly
labels a partial pool build; a complete package requires finished build state.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import pickle
import sqlite3
import subprocess
import sys
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def digest(path):
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            result.update(block)
    return result.hexdigest()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--graphs", type=Path, required=True)
    parser.add_argument("--pools", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--sample", action="store_true")
    parser.add_argument("--offline-demo", type=Path)
    args = parser.parse_args(argv)
    from runline.pools import FORMAT_VERSION, decode_pool
    from runline.scoring import MAX_REPEATED

    commit = subprocess.check_output(["git", "rev-parse", f"{args.commit}^{{commit}}"], cwd=ROOT, text=True).strip()
    manifest = json.loads((args.graphs / "manifest.json").read_text())
    area = next(a for a in manifest["areas"] if a["slug"] == args.pools.stem)
    graph_path = args.graphs / area["file"]
    graph_hash = digest(graph_path)
    with gzip.open(graph_path, "rb") as stream:
        graph = pickle.load(stream)
    rows, loops, surfaces = 0, 0, set()
    with sqlite3.connect(f"file:{args.pools.resolve()}?mode=ro", uri=True) as database:
        if database.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError("pool database integrity check failed")
        meta = {k: json.loads(v) for k, v in database.execute("SELECT key, value FROM meta")}
        if meta.get("format") != FORMAT_VERSION or meta.get("graph") != graph_hash[:16]:
            raise ValueError("pool format or graph hash does not match")
        for surface, expected, blob in database.execute("SELECT surface, loops, pool FROM pools"):
            pool = decode_pool(blob)
            if len(pool["loops"]) != expected or pool["origin_node"] not in graph:
                raise ValueError("pool header does not match its content")
            for loop in pool["loops"]:
                nodes = loop["nodes"]
                if len(nodes) < 4 or nodes[0] != nodes[-1]:
                    raise ValueError("pool contains an open or empty loop")
                if any(not graph.has_edge(u, v) for u, v in zip(nodes, nodes[1:])):
                    raise ValueError("pool names an edge absent from its graph")
                if loop["metrics"]["repeated_fraction"] > MAX_REPEATED + 0.00001:
                    raise ValueError("pool contains a loop serving would reject for repetition")
            rows += 1
            loops += expected
            surfaces.add(surface)
    if not rows:
        raise ValueError("pool file is empty")
    if not args.sample:
        progress = json.loads((args.pools.parent / "progress.json").read_text())
        if (progress.get("cells") or progress.get("limit_cells") or progress["failed"]
                or len(progress["completed"]) != progress["shards"]
                or surfaces != {"road", "mixed", "trail"}):
            raise ValueError("complete area build has not finished")
    changed = subprocess.check_output([
        "git", "diff", meta["commit"], commit, "--", "src/runline/osm.py", "src/runline/models.py",
        "src/runline/scoring.py", "scripts/precompute_pools.py",
    ], cwd=ROOT, text=True)
    if changed:
        raise ValueError("application routing/generation differs from the pool build")
    report = {"commit": commit, "pool_build_commit": meta["commit"], "graph": graph_hash,
              "pools": digest(args.pools), "pool_count": rows, "loops": loops,
              "surfaces": sorted(surfaces), "pool_scope": "sample" if args.sample else "complete area"}
    snapshot = subprocess.check_output([
        "git", "archive", commit, "app.py", "src", "web", "requirements.txt", "README.md",
        "scripts/presentation_check.py", "scripts/pool_smoke.py",
    ], cwd=ROOT)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(".tmp")
    prefix = f"runline-{commit[:12]}"

    def add_bytes(archive, name, content, mode=0o644):
        encoded = content.encode() if isinstance(content, str) else content
        member = tarfile.TarInfo(f"{prefix}/{name}")
        member.size, member.mode = len(encoded), mode
        archive.addfile(member, io.BytesIO(encoded))

    with tarfile.open(temporary, "w:gz") as target:
        with tarfile.open(fileobj=io.BytesIO(snapshot)) as source:
            for member in source.getmembers():
                stream = source.extractfile(member) if member.isfile() else None
                member.name = f"{prefix}/{member.name}"
                target.addfile(member, stream)
        for key in ("file", "drive_file", "starts_file", "places_file"):
            if area.get(key):
                target.add(args.graphs / area[key], arcname=f"{prefix}/graphs/{area[key]}")
        add_bytes(target, "graphs/manifest.json", json.dumps({"areas": [area]}, indent=2))
        target.add(args.pools, arcname=f"{prefix}/pools/{args.pools.name}")
        add_bytes(target, "release.json", json.dumps(report, indent=2))
        add_bytes(target, "NOTICE", "Map data © OpenStreetMap contributors (ODbL).\nTerrain © Copernicus DEM GLO-90.\nLeaflet license: web/vendor/leaflet-1.9.4/LICENSE\n")
        add_bytes(target, "start.sh", f'''#!/usr/bin/env bash
set -euo pipefail
bundle_dir=$(cd "$(dirname "$0")" && pwd)
cd "$bundle_dir"
export PYTHONPATH="$bundle_dir/src"
export RUNLINE_BUILD_SHA={commit}
export RUNLINE_ALLOW_OSM_DOWNLOAD=0
exec "${{RUNLINE_PYTHON:-$bundle_dir/.venv/bin/python}}" -m uvicorn runline.api:app --host "${{RUNLINE_HOST:-127.0.0.1}}" --port "${{RUNLINE_PORT:-8765}}"
''', 0o755)
        if args.offline_demo:
            target.add(args.offline_demo, arcname=f"{prefix}/saved-demo.html")
    temporary.replace(args.output)
    report["artifact"] = args.output.name
    report["artifact_sha256"] = digest(args.output)
    args.output.with_suffix(args.output.suffix + ".json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
