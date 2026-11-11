"""Rebuild a shipped walk graph into a separate artifact directory.

Uses the manifest's existing center and radius, keeps signalized crossings,
bakes elevation, and copies the matching area's drive/start/place assets.
The existing graph and its pools stay usable while replacement pools build.

    python scripts/rebuild_area.py --area charlottesville-va \
        --output output/presentation/graphs --osm-cache ../runline/cache
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import importlib.metadata
import json
import pickle
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--area", default="charlottesville-va")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--osm-cache", type=Path, default=ROOT / "cache")
    parser.add_argument("--dem-cache", type=Path, default=ROOT / "build-cache/dem")
    args = parser.parse_args(argv)

    import osmnx as ox
    from osmnx import _overpass

    from runline.coverage import graphs_root
    from runline.elevation import enrich_graph_copernicus
    from runline.osm import is_signal
    from runline.pbf import tile_polygons, walk_graph_from_responses

    source = graphs_root()
    if args.output.resolve() == source.resolve():
        parser.error("--output must differ from the shipped graphs directory")
    if (args.output / "manifest.json").exists():
        parser.error("output already contains a graph build; choose a new directory")
    manifest = json.loads((source / "manifest.json").read_text())
    area = next((a for a in manifest["areas"] if a["slug"] == args.area), None)
    if area is None:
        parser.error(f"no shipped area {args.area!r}")

    ox.settings.cache_folder = args.osm_cache
    ox.settings.use_cache = True
    polygon, buffered = tile_polygons(area["latitude"], area["longitude"], area["radius_meters"])
    print(f"Building {args.area} with signal nodes preserved", flush=True)
    graph = walk_graph_from_responses(
        _overpass._download_overpass_network(buffered, "walk", None), polygon, buffered
    )
    if graph is None or not graph.number_of_nodes():
        raise RuntimeError("no walk graph was built")
    print(f"Baking elevation on {graph.number_of_nodes():,} nodes", flush=True)
    baked = enrich_graph_copernicus(graph, args.dem_cache)
    coverage = baked / graph.number_of_nodes()
    if coverage < 0.95:
        raise RuntimeError(f"elevation coverage {coverage:.1%} is below 95%; no graph published")
    graph.graph["elevation_baked"] = True

    payload = gzip.compress(pickle.dumps(graph, protocol=5), compresslevel=6, mtime=0)
    report = {
        "area": args.area,
        "previous_graph": hashlib.sha256((source / area["file"]).read_bytes()).hexdigest()[:16],
        "graph": hashlib.sha256(payload).hexdigest()[:16],
        "nodes": graph.number_of_nodes(),
        "edges": graph.number_of_edges(),
        "signal_nodes": sum(is_signal(d) for _, d in graph.nodes(data=True)),
        "crosswalk_signal_nodes": sum(
            is_signal(d) and d.get("highway") != "traffic_signals" for _, d in graph.nodes(data=True)
        ),
        "elevation_nodes": baked,
        "elevation_coverage": coverage,
        "versions": {name: importlib.metadata.version(name) for name in ("networkx", "shapely", "osmnx", "rasterio")},
        "commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
    }
    args.output.mkdir(parents=True, exist_ok=True)
    for key in ("drive_file", "starts_file", "places_file"):
        if area.get(key):
            shutil.copy2(source / area[key], args.output / area[key])
    destination = args.output / area["file"]
    temporary = destination.with_suffix(".tmp")
    temporary.write_bytes(payload)
    temporary.replace(destination)
    (args.output / "build-report.json").write_text(json.dumps(report, indent=2) + "\n")
    # Manifest last: serving never discovers a partially published build.
    (args.output / "manifest.json").write_text(json.dumps({"areas": [area]}, indent=2) + "\n")
    print(json.dumps(report, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
