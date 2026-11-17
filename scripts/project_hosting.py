"""Project the disk and memory serving the top 50 metros needs, from the DC pilot.

    python scripts/project_hosting.py
    python scripts/project_hosting.py --cache 16 --workers 1 --json hosting.json
    python scripts/project_hosting.py --group dc-baltimore=washington-dc,baltimore-md

Reads config/metros.json (each metro's tiles and planned cells) and
config/metro-batches.json (each batch's metros and its cell share) and scales
what the Washington, DC pilot measured on the Oracle VM (docs/handoff.md,
task 4). Nothing here is measured on the metro itself; replace the constants
with scripts/measure_tile_graphs.py results once a batch's tile graphs exist.

Scaling assumptions:

- Tile graph disk and memory scale with tile count. DC: 230 tile graphs in
  4.2 GB (18.3 MB a tile) and about 650 MB resident per tile once serving has
  used it. A metro's tiles are all built, including the few that own no cells.
- Pool disk scales with planned cells. DC: 2.3 GB for 93,183 planned cells,
  of which 72,237 got pools (77.5%). The low/mid case keeps DC's 24.7 KB per
  planned cell; the high case assumes every planned cell gets pools at DC's
  31.8 KB per pooled cell.
- Tile weight is a factor on DC's per-tile figures: low 0.6 (a Charlottesville
  density tile: its 12 km graph is 7.9 MB, about 10.4 MB at a tile's 13.8 km),
  mid 1.0, high 1.5. Metros whose population per planned cell is at least
  --dense-ratio times DC's get a high of 2.0, since denser street grids and
  sidewalk mapping mean more nodes per tile. This is a guess to be replaced by
  measurement; population density is only a proxy for street density.
- RAM is per worker, because each uvicorn worker holds its own graph cache:
  an OS reserve, plus per worker an overhead (baseline, live-planning crops
  and memos) and --cache tiles at 650 MB times the tile factor. A batch uses
  the largest factor among its metros, since the hot tiles are the dense cores.

Sizes are decimal GB, as the pilot figures were reported. Once a batch is
built, pass its measured --graph-mb-per-tile (build_tile_graphs.py summary)
and --tile-ram-mb (measure_tile_graphs.py) to replace DC's figures; the tile
factors then spread around the measured value instead.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Washington, DC pilot on the Oracle VM, measured November 11 (docs/handoff.md).
DC_SLUG = "washington-dc"
DC_TILES = 230
DC_GRAPHS_GB = 4.2
DC_PLANNED_CELLS = 93_183
DC_POOLED_CELLS = 72_237
DC_POOLS_GB = 2.3
TILE_RAM_GB = 0.65

GRAPH_GB_PER_TILE = DC_GRAPHS_GB / DC_TILES
POOL_GB_PER_PLANNED_CELL = DC_POOLS_GB / DC_PLANNED_CELLS
POOL_GB_PER_POOLED_CELL = DC_POOLS_GB / DC_POOLED_CELLS

LOW_FACTOR = 0.6
MID_FACTOR = 1.0
HIGH_FACTOR = 1.5
DENSE_HIGH_FACTOR = 2.0
DEFAULT_DENSE_RATIO = 1.25

# The team's VM.Standard.A1.Flex.
VM_DISK_GB = 190.0
VM_RAM_GB = 24.0
# Ubuntu, the checkout, its virtualenv and the OSM/elevation caches.
DEFAULT_DISK_RESERVE_GB = 15.0
# Ubuntu, Caddy and sshd (deploy/provision.sh assumes about 500 MB).
DEFAULT_OS_RAM_GB = 1.0
# A worker's own memory beyond its cached tiles: about 70 MB at start, plus
# up to four prepared live-planning crops and the per-graph memos. On the
# 40k-node Charlottesville graph that was 350-500 MB (deploy/README.md);
# tiles have 1-3x the nodes.
DEFAULT_WORKER_OVERHEAD_GB = 1.5


@dataclass(frozen=True)
class Range:
    low: float
    mid: float
    high: float

    def __add__(self, other: "Range") -> "Range":
        return Range(self.low + other.low, self.mid + other.mid, self.high + other.high)

    def rounded(self, digits: int = 1) -> "Range":
        return Range(round(self.low, digits), round(self.mid, digits), round(self.high, digits))


@dataclass(frozen=True)
class Projection:
    name: str
    metros: list[str]
    tiles: int
    cells: int
    dense: bool
    graphs_gb: Range
    pools_gb: Range
    disk_gb: Range
    ram_gb: Range
    hot_tiles: Range  # tiles per worker that fit the VM's RAM


@dataclass(frozen=True)
class Settings:
    cache: int = 16
    workers: int = 1
    dense_ratio: float = DEFAULT_DENSE_RATIO
    os_ram_gb: float = DEFAULT_OS_RAM_GB
    worker_overhead_gb: float = DEFAULT_WORKER_OVERHEAD_GB
    vm_ram_gb: float = VM_RAM_GB
    # DC's per-tile figures unless a measured batch replaces them.
    graph_gb_per_tile: float = GRAPH_GB_PER_TILE
    tile_ram_gb: float = TILE_RAM_GB


def tile_factors(dense: bool) -> Range:
    return Range(LOW_FACTOR, MID_FACTOR, DENSE_HIGH_FACTOR if dense else HIGH_FACTOR)


def dense_metros(config: dict, ratio: float) -> set[str]:
    """Metros whose population per planned cell is at least ``ratio`` times DC's.

    With no DC in the config (a fixture), the median metro stands in for it.
    """

    density = {metro["slug"]: metro["population"] / metro["cells"] for metro in config["metros"] if metro["cells"]}
    if not density:
        return set()
    reference = density.get(DC_SLUG) or sorted(density.values())[len(density) // 2]
    return {slug for slug, value in density.items() if value >= ratio * reference}


def graphs_gb(tiles: int, factors: Range, settings: Settings) -> Range:
    base = tiles * settings.graph_gb_per_tile
    return Range(base * factors.low, base * factors.mid, base * factors.high)


def pools_gb(cells: int) -> Range:
    planned = cells * POOL_GB_PER_PLANNED_CELL
    return Range(planned, planned, cells * POOL_GB_PER_POOLED_CELL)


def ram_gb(factors: Range, settings: Settings) -> Range:
    def one(factor: float) -> float:
        per_worker = settings.worker_overhead_gb + settings.cache * settings.tile_ram_gb * factor
        return settings.os_ram_gb + settings.workers * per_worker

    return Range(one(factors.low), one(factors.mid), one(factors.high))


def hot_tiles(factors: Range, settings: Settings) -> Range:
    """Tiles each worker can cache before the VM runs out of RAM.

    The low end of the range is the heavy-tile case, so ``low`` here pairs
    with ``high`` tile weight.
    """

    def one(factor: float) -> float:
        spare = settings.vm_ram_gb - settings.os_ram_gb - settings.workers * settings.worker_overhead_gb
        return max(0, math.floor(spare / settings.workers / (settings.tile_ram_gb * factor)))

    return Range(one(factors.high), one(factors.mid), one(factors.low))


def project(name: str, metros: list[dict], cells: int, dense: set[str], settings: Settings) -> Projection:
    slugs = [metro["slug"] for metro in metros]
    graphs = Range(0.0, 0.0, 0.0)
    tiles = 0
    for metro in metros:
        count = len(metro["tiles"])
        tiles += count
        graphs = graphs + graphs_gb(count, tile_factors(metro["slug"] in dense), settings)
    heavy = any(slug in dense for slug in slugs)
    factors = tile_factors(heavy)
    pools = pools_gb(cells)
    return Projection(
        name=name,
        metros=slugs,
        tiles=tiles,
        cells=cells,
        dense=heavy,
        graphs_gb=graphs.rounded(),
        pools_gb=pools.rounded(),
        disk_gb=(graphs + pools).rounded(),
        ram_gb=ram_gb(factors, settings).rounded(),
        hot_tiles=hot_tiles(factors, settings),
    )


def projections(config: dict, batches: dict | None, groups: dict[str, list[str]], settings: Settings) -> dict:
    by_slug = {metro["slug"]: metro for metro in config["metros"]}
    dense = dense_metros(config, settings.dense_ratio)
    per_metro = [project(metro["slug"], [metro], metro["cells"], dense, settings) for metro in config["metros"]]
    per_batch = []
    for batch in (batches or {}).get("batches", []):
        unknown = [slug for slug in batch["metros"] if slug not in by_slug]
        if unknown:
            raise ValueError(f"batch {batch['name']} names unknown metros {unknown}")
        members = [by_slug[slug] for slug in batch["metros"]]
        # A batch's recorded cells are its share of the all-metro plan, which
        # drops cells two metros share; fall back to the metros' own counts.
        cells = batch.get("cells") or sum(metro["cells"] for metro in members)
        per_batch.append(project(batch["name"], members, cells, dense, settings))
    per_group = []
    for name, slugs in groups.items():
        unknown = [slug for slug in slugs if slug not in by_slug]
        if unknown:
            raise ValueError(f"group {name} names unknown metros {unknown}")
        members = [by_slug[slug] for slug in slugs]
        per_group.append(project(name, members, sum(metro["cells"] for metro in members), dense, settings))
    if per_batch:
        all_cells = sum(batch.cells for batch in per_batch)
    else:
        all_cells = config.get("totals", {}).get("unique_cells") or sum(metro["cells"] for metro in config["metros"])
    everything = project("all", config["metros"], all_cells, dense, settings)
    return {
        "settings": asdict(settings),
        "assumptions": {
            "graph_gb_per_tile": round(settings.graph_gb_per_tile, 5),
            "tile_ram_gb": settings.tile_ram_gb,
            "pool_kb_per_planned_cell": round(POOL_GB_PER_PLANNED_CELL * 1e6, 2),
            "pool_kb_per_pooled_cell": round(POOL_GB_PER_POOLED_CELL * 1e6, 2),
            "tile_factors": {"low": LOW_FACTOR, "mid": MID_FACTOR, "high": HIGH_FACTOR, "dense_high": DENSE_HIGH_FACTOR},
            "dense_metros": sorted(dense),
            "vm_disk_gb": VM_DISK_GB,
            "vm_ram_gb": settings.vm_ram_gb,
        },
        "batches": [asdict(item) for item in per_batch],
        "groups": [asdict(item) for item in per_group],
        "all": asdict(everything),
        "metros": [asdict(item) for item in per_metro],
    }


def _span(value: dict) -> str:
    return f"{value['mid']:,.1f} ({value['low']:,.1f}-{value['high']:,.1f})"


def _hot(value: dict) -> str:
    return f"{value['mid']:,} ({value['low']:,}-{value['high']:,})"


def markdown(result: dict) -> str:
    settings = result["settings"]
    header = (
        f"| Name | Metros | Tiles | Cells | Tile graphs GB | Pools GB | Disk GB | "
        f"RAM GB, cache {settings['cache']} x {settings['workers']} worker(s) | Hot tiles per worker in {settings['vm_ram_gb']:g} GB |\n"
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|"
    )

    def row(item: dict, metros: str) -> str:
        name = item["name"] + (" *" if item["dense"] else "")
        return (
            f"| {name} | {metros} | {item['tiles']:,} | {item['cells']:,} | {_span(item['graphs_gb'])} | "
            f"{_span(item['pools_gb'])} | {_span(item['disk_gb'])} | {_span(item['ram_gb'])} | {_hot(item['hot_tiles'])} |"
        )

    lines = ["Mid (low-high). * dense: tile weight up to 2.0x DC.", "", header]
    for item in result["batches"] + result["groups"]:
        lines.append(row(item, str(len(item["metros"]))))
    lines.append(row(result["all"], str(len(result["all"]["metros"]))))
    lines += ["", header.replace("| Metros ", "| Rank ", 1)]
    for rank, item in enumerate(result["metros"], start=1):
        lines.append(row(item, str(rank)))
    return "\n".join(lines) + "\n"


def parse_groups(values: list[str]) -> dict[str, list[str]]:
    groups = {}
    for value in values:
        name, _, slugs = value.partition("=")
        if not name or not slugs:
            raise argparse.ArgumentTypeError(f"--group wants NAME=slug,slug, not {value!r}")
        groups[name] = [slug.strip() for slug in slugs.split(",") if slug.strip()]
    return groups


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="project_hosting", description=__doc__.split("\n\n")[0])
    parser.add_argument("--metros", type=Path, default=ROOT / "config" / "metros.json")
    parser.add_argument("--batches", type=Path, default=ROOT / "config" / "metro-batches.json")
    parser.add_argument("--cache", type=int, default=16, help="RUNLINE_TILE_GRAPHS_CACHE per worker")
    parser.add_argument("--workers", type=int, default=1, help="uvicorn workers, each with its own cache")
    parser.add_argument("--dense-ratio", type=float, default=DEFAULT_DENSE_RATIO)
    parser.add_argument("--worker-overhead-gb", type=float, default=DEFAULT_WORKER_OVERHEAD_GB)
    parser.add_argument("--os-ram-gb", type=float, default=DEFAULT_OS_RAM_GB)
    parser.add_argument("--vm-ram-gb", type=float, default=VM_RAM_GB)
    parser.add_argument(
        "--graph-mb-per-tile", type=float, help="measured mean tile graph file size (default: DC's 18.3 MB)"
    )
    parser.add_argument("--tile-ram-mb", type=float, help="measured resident MB per used tile (default: DC's 650)")
    parser.add_argument("--group", action="append", default=[], help="NAME=slug,slug: also total these metros")
    parser.add_argument("--json", type=Path, help="also write the projection as JSON here ('-' for stdout only)")
    arguments = parser.parse_args(argv)
    if arguments.cache < 1 or arguments.workers < 1:
        parser.error("--cache and --workers must be at least 1")

    config = json.loads(arguments.metros.read_text(encoding="utf-8"))
    batches = json.loads(arguments.batches.read_text(encoding="utf-8")) if arguments.batches.exists() else None
    settings = Settings(
        cache=arguments.cache,
        workers=arguments.workers,
        dense_ratio=arguments.dense_ratio,
        os_ram_gb=arguments.os_ram_gb,
        worker_overhead_gb=arguments.worker_overhead_gb,
        vm_ram_gb=arguments.vm_ram_gb,
        graph_gb_per_tile=arguments.graph_mb_per_tile / 1000 if arguments.graph_mb_per_tile else GRAPH_GB_PER_TILE,
        tile_ram_gb=arguments.tile_ram_mb / 1000 if arguments.tile_ram_mb else TILE_RAM_GB,
    )
    try:
        result = projections(config, batches, parse_groups(arguments.group), settings)
    except (ValueError, argparse.ArgumentTypeError) as error:
        print(error, file=sys.stderr)
        return 1
    if arguments.json and str(arguments.json) == "-":
        print(json.dumps(result, indent=1))
        return 0
    print(markdown(result), end="")
    if arguments.json:
        arguments.json.write_text(json.dumps(result, indent=1) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
