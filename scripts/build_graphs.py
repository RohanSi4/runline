"""Build the precomputed walk graphs that Runline serves at request time.

Reads config/areas.json, downloads one OSM walk graph per area, and writes it
to graphs/<slug>.pkl.gz alongside a manifest the app reads at startup.

Run this whenever the area list changes; the outputs are committed so that a
deployment never needs to reach Overpass.

    python scripts/build_graphs.py            # skip areas already built
    python scripts/build_graphs.py --force    # rebuild everything
    python scripts/build_graphs.py --only charlottesville-va
"""

from __future__ import annotations

import argparse
import gzip
import json
import pickle
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

CONFIG_PATH = PROJECT_ROOT / "config" / "areas.json"
GRAPHS_DIR = PROJECT_ROOT / "graphs"

# Measured on a full 5 mile request: 57k nodes peaks near 490MB RSS, 118k nodes
# peaks near 1186MB and would OOM a 1GB function. Roughly 8MB of resident memory
# per 1000 nodes, so 70k leaves headroom under a 1GB budget. Dense cities need a
# smaller radius, not a bigger memory budget.
NODE_WARNING_THRESHOLD = 70_000


def build_area(area: dict, *, force: bool) -> dict:
    import osmnx as ox

    slug = area["slug"]
    radius = float(area["radius_meters"])
    destination = GRAPHS_DIR / f"{slug}.pkl.gz"

    if destination.exists() and not force:
        print(f"  {slug}: already built, skipping (use --force to rebuild)")
        latitude, longitude = area["latitude"], area["longitude"]
    else:
        started = time.time()
        if "latitude" in area and "longitude" in area:
            latitude, longitude = float(area["latitude"]), float(area["longitude"])
        else:
            latitude, longitude = ox.geocoder.geocode(area["query"])
            latitude, longitude = float(latitude), float(longitude)

        graph = ox.graph.graph_from_point(
            (latitude, longitude),
            dist=radius,
            network_type="walk",
            simplify=True,
            retain_all=True,
        )
        payload = gzip.compress(pickle.dumps(graph, protocol=5), 6)
        GRAPHS_DIR.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(payload)

        nodes = graph.number_of_nodes()
        print(
            f"  {slug}: {nodes} nodes, {len(payload) / 1048576:.1f}MB gz, "
            f"built in {time.time() - started:.0f}s"
        )
        if nodes > NODE_WARNING_THRESHOLD:
            print(
                f"    WARNING: {nodes} nodes exceeds {NODE_WARNING_THRESHOLD}. "
                "Reduce radius_meters for this area."
            )

    return {
        "slug": slug,
        "label": area["label"],
        "latitude": latitude,
        "longitude": longitude,
        "radius_meters": radius,
        "file": f"{slug}.pkl.gz",
    }


def main() -> int:
    parser = argparse.ArgumentParser(prog="build_graphs")
    parser.add_argument("--force", action="store_true", help="rebuild existing graphs")
    parser.add_argument("--only", help="build a single area by slug")
    arguments = parser.parse_args()

    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    selected = [
        area
        for area in config["areas"]
        if arguments.only is None or area["slug"] == arguments.only
    ]
    if not selected:
        print(f"no area matching {arguments.only!r} in {CONFIG_PATH}")
        return 1

    print(f"building {len(selected)} area(s) into {GRAPHS_DIR}")
    entries = [build_area(area, force=arguments.force) for area in selected]

    # A partial run must not drop areas the manifest already lists.
    manifest_path = GRAPHS_DIR / "manifest.json"
    existing = {}
    if manifest_path.exists():
        existing = {
            entry["slug"]: entry
            for entry in json.loads(manifest_path.read_text(encoding="utf-8"))["areas"]
        }
    for entry in entries:
        existing[entry["slug"]] = entry

    ordered = sorted(existing.values(), key=lambda entry: entry["label"])
    manifest_path.write_text(
        json.dumps({"version": 1, "areas": ordered}, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {manifest_path} with {len(ordered)} area(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
