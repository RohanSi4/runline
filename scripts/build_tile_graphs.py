"""Build metro tile walk graphs from the Geofabrik US extract.

Three steps, the first two once per US extract:

1. Write osmium extract configs, one per batch of tiles from config/metros.json,
   plus the commands to run them (osmium-tool, see slurm/extract.sbatch):

       python scripts/build_tile_graphs.py configs \\
           --pbf /bigtemp/$USER/us-latest.osm.pbf --extracts /bigtemp/$USER/tiles

   The commands first keep only highway ways (11 GB down to a few), then cut
   one small .osm.pbf per tile covering its disc plus osmnx's 500 m buffer.

2. Run them (slurm/extract.sbatch).

3. Build each tile's graph, sharded for a SLURM array (slurm/build_tiles.sbatch):

       python scripts/build_tile_graphs.py build \\
           --extracts /bigtemp/$USER/tiles --output /bigtemp/$USER/tile-graphs \\
           --shard-index 0 --shard-count 400

   Each tile writes <slug>.pkl.gz and <slug>.json (node and edge counts, time,
   elevation coverage); built tiles are skipped. ``summary`` totals the .json
   files and lists the largest tiles, which sets memory for the precompute.

To check the builder against the Overpass build of a shipped area:

    osmium extract -b <west,south,east,north from "bounds"> virginia-latest.osm.pbf -o cville.osm.pbf
    python scripts/build_tile_graphs.py bounds --area charlottesville-va
    python scripts/build_tile_graphs.py build --area charlottesville-va \\
        --pbf cville.osm.pbf --output build-cache/pbf-graphs
    python scripts/build_tile_graphs.py compare \\
        build-cache/pbf-graphs/charlottesville-va.pkl.gz graphs/charlottesville-va.pkl.gz

Needs the build install (``pip install -e '.[dev,build]'``): osmnx and
pyosmium. osmium-tool is a separate binary, only for step 2.
"""

from __future__ import annotations

import argparse
import gzip
import json
import math
import os
import pickle
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

METROS_PATH = ROOT / "config" / "metros.json"
AREAS_PATH = ROOT / "config" / "areas.json"
DEFAULT_DEM_DIR = ROOT / "build-cache" / "dem"
# osmium extract keeps one output buffer per extract while it reads the file;
# its docs suggest staying in the low hundreds per run.
DEFAULT_BATCH_SIZE = 300


def metro_tiles(metros_path: Path, only: list[str] | None = None) -> list[dict]:
    config = json.loads(metros_path.read_text(encoding="utf-8"))
    tiles = []
    for metro in config["metros"]:
        if only and metro["slug"] not in only:
            continue
        tiles.extend(metro["tiles"])
    return tiles


def area_tile(slug: str) -> dict:
    """A shipped area from config/areas.json, shaped like a tile entry."""

    for area in json.loads(AREAS_PATH.read_text(encoding="utf-8"))["areas"]:
        if area["slug"] == slug:
            if "latitude" not in area:
                manifest = json.loads((ROOT / "graphs" / "manifest.json").read_text(encoding="utf-8"))
                known = next(entry for entry in manifest["areas"] if entry["slug"] == slug)
                area = {**area, "latitude": known["latitude"], "longitude": known["longitude"]}
            return area
    raise SystemExit(f"no area {slug!r} in {AREAS_PATH}")


def write_configs(arguments) -> int:
    from runline.pbf import extract_bounds

    tiles = metro_tiles(arguments.metros, arguments.metro)
    extracts = arguments.extracts.resolve()
    extracts.mkdir(parents=True, exist_ok=True)
    highways = extracts / "highways.osm.pbf"
    commands = [
        "set -euo pipefail",
        f"osmium tags-filter --overwrite -o {highways} {arguments.pbf} w/highway",
    ]
    batches = math.ceil(len(tiles) / arguments.batch_size)
    for batch in range(batches):
        chunk = tiles[batch * arguments.batch_size : (batch + 1) * arguments.batch_size]
        config = {
            "directory": str(extracts),
            "extracts": [
                {
                    "output": f"{tile['slug']}.osm.pbf",
                    "bbox": list(extract_bounds(tile["latitude"], tile["longitude"], tile["radius_meters"])),
                }
                for tile in chunk
            ],
        }
        path = extracts / f"extract-{batch:03d}.json"
        path.write_text(json.dumps(config, indent=1) + "\n", encoding="utf-8")
        commands.append(f"osmium extract --overwrite -s complete_ways -c {path} {highways}")
    script = extracts / "extract.sh"
    script.write_text("\n".join(commands) + "\n", encoding="utf-8")
    print(f"{len(tiles)} tiles in {batches} extract configs; run: bash {script}")
    return 0


def bake(graph, dem_dir: Path) -> float:
    from runline.elevation import enrich_graph_copernicus

    baked = enrich_graph_copernicus(graph, dem_dir)
    total = graph.number_of_nodes()
    # Same rule as build_graphs.py: the planner needs 95% coverage.
    graph.graph["elevation_baked"] = baked >= 0.95 * total
    return baked / total if total else 0.0


def build_one(tile: dict, pbf: Path, output: Path, dem_dir: Path) -> dict | None:
    from runline.pbf import walk_graph_from_pbf

    started = time.time()
    graph = walk_graph_from_pbf(pbf, tile["latitude"], tile["longitude"], tile["radius_meters"])
    stats = {"slug": tile["slug"], "metro": tile.get("metro"), "seconds": None}
    if graph is None:
        stats.update(nodes=0, edges=0, empty=True)
    else:
        coverage = bake(graph, dem_dir)
        payload = gzip.compress(pickle.dumps(graph, protocol=5), 6)
        temporary = output / f"{tile['slug']}.pkl.gz.tmp"
        temporary.write_bytes(payload)
        os.replace(temporary, output / f"{tile['slug']}.pkl.gz")
        stats.update(
            nodes=graph.number_of_nodes(),
            edges=graph.number_of_edges(),
            elevation_coverage=round(coverage, 4),
            megabytes=round(len(payload) / 1048576, 1),
        )
    stats["seconds"] = round(time.time() - started, 1)
    (output / f"{tile['slug']}.json").write_text(json.dumps(stats) + "\n", encoding="utf-8")
    return stats


def build(arguments) -> int:
    output = arguments.output
    output.mkdir(parents=True, exist_ok=True)
    if arguments.area:
        if not arguments.pbf:
            raise SystemExit("--area needs --pbf")
        tiles = [area_tile(arguments.area)]
    else:
        if not arguments.extracts:
            raise SystemExit("build needs --extracts (or --area with --pbf)")
        tiles = metro_tiles(arguments.metros, arguments.metro)
        tiles = tiles[arguments.shard_index :: arguments.shard_count]

    for tile in tiles:
        if (output / f"{tile['slug']}.json").exists():
            print(f"  {tile['slug']}: already built, skipping")
            continue
        pbf = arguments.pbf if arguments.area else arguments.extracts / f"{tile['slug']}.osm.pbf"
        if not pbf.exists():
            print(f"  {tile['slug']}: no extract at {pbf}")
            return 1
        stats = build_one(tile, pbf, output, arguments.dem_dir)
        print(f"  {tile['slug']}: {stats['nodes']} nodes, {stats['edges']} edges in {stats['seconds']}s", flush=True)
    return 0


def summary(arguments) -> int:
    rows = [json.loads(path.read_text()) for path in sorted(arguments.output.glob("*.json"))]
    if not rows:
        print(f"no tile stats under {arguments.output}")
        return 1
    built = [row for row in rows if not row.get("empty")]
    nodes = sorted(row["nodes"] for row in built)
    print(
        f"{len(rows)} tiles ({len(rows) - len(built)} empty), {sum(nodes):,} nodes, "
        f"median {nodes[len(nodes) // 2]:,}, max {nodes[-1]:,}, "
        f"{sum(row.get('megabytes', 0) for row in built):,.0f} MB, "
        f"{sum(row['seconds'] for row in rows) / 3600:.1f} build-hours"
    )
    low = [row["slug"] for row in built if row.get("elevation_coverage", 1) < 0.95]
    if low:
        print(f"{len(low)} tiles under 95% elevation coverage: {', '.join(low[:10])}")
    for row in sorted(built, key=lambda row: -row["nodes"])[:10]:
        print(f"  {row['slug']}: {row['nodes']:,} nodes, {row['seconds']}s")
    return 0


def bounds(arguments) -> int:
    from runline.pbf import extract_bounds

    tile = area_tile(arguments.area)
    west, south, east, north = extract_bounds(tile["latitude"], tile["longitude"], tile["radius_meters"])
    print(f"{west:.5f},{south:.5f},{east:.5f},{north:.5f}")
    return 0


def load(path: Path):
    with gzip.open(path, "rb") as handle:
        return pickle.load(handle)


def compare_graphs(first, second) -> dict:
    def length(graph):
        return sum(float(data.get("length", 0)) for _, _, data in graph.edges(data=True))

    shared = set(first.nodes) & set(second.nodes)
    return {
        "nodes": (first.number_of_nodes(), second.number_of_nodes()),
        "edges": (first.number_of_edges(), second.number_of_edges()),
        "km": (round(length(first) / 1000, 1), round(length(second) / 1000, 1)),
        "shared_nodes": len(shared),
        "shared_of_smaller": round(len(shared) / max(1, min(len(first), len(second))), 4),
    }


def compare(arguments) -> int:
    result = compare_graphs(load(arguments.first), load(arguments.second))
    for key, value in result.items():
        print(f"{key}: {value}")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="build_tile_graphs")
    commands = parser.add_subparsers(dest="command", required=True)

    configs = commands.add_parser("configs", help="write osmium extract configs for the metro tiles")
    configs.add_argument("--pbf", type=Path, required=True, help="the US extract, e.g. us-latest.osm.pbf")
    configs.add_argument("--extracts", type=Path, required=True, help="directory for configs and tile extracts")
    configs.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)

    builder = commands.add_parser("build", help="build tile walk graphs")
    builder.add_argument("--extracts", type=Path, help="directory of <slug>.osm.pbf tile extracts")
    builder.add_argument("--output", type=Path, required=True)
    builder.add_argument("--area", help="build a shipped area from config/areas.json instead of tiles")
    builder.add_argument("--pbf", type=Path, help="extract to read with --area")
    builder.add_argument("--dem-dir", type=Path, default=DEFAULT_DEM_DIR)
    builder.add_argument(
        "--shard-index", type=int, default=int(os.environ.get("SLURM_ARRAY_TASK_ID", 0))
    )
    builder.add_argument("--shard-count", type=int, default=1)

    for sub in (configs, builder):
        sub.add_argument("--metros", type=Path, default=METROS_PATH)
        sub.add_argument("--metro", nargs="+", help="only these metro slugs")

    totals = commands.add_parser("summary", help="total the per-tile stats")
    totals.add_argument("--output", type=Path, required=True)

    area_bounds = commands.add_parser("bounds", help="print the extract bbox for a shipped area")
    area_bounds.add_argument("--area", required=True)

    comparison = commands.add_parser("compare", help="compare two pickled walk graphs")
    comparison.add_argument("first", type=Path)
    comparison.add_argument("second", type=Path)

    arguments = parser.parse_args(argv)
    if arguments.command == "build" and not 0 <= arguments.shard_index < arguments.shard_count:
        parser.error("--shard-index must be in [0, --shard-count)")
    handlers = {"configs": write_configs, "build": build, "summary": summary, "bounds": bounds, "compare": compare}
    return handlers[arguments.command](arguments)


if __name__ == "__main__":
    raise SystemExit(main())
