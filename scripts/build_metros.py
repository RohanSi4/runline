"""Pick the largest US metros and cut each into graph tiles for precompute.

Inputs are two public Census files, both saved under build-cache/census/:

- 2020 urban-area boundaries, the TIGER/Line shapefile ``tl_<year>_us_uac20.zip``
  (read as is, no need to unzip).
- 2020 urban-area populations, in any of three forms:
  the Census Data API response saved as .json
  (api.census.gov/data/2020/dec/dhc?get=NAME,P1_001N&for=urban%20area:*),
  a .csv with a population column and a GEOID or name column, or the
  Federal Register list of 2020 urban areas (2022-28286) saved as .txt.

Outputs, both committed:

- ``config/metros.json``: one entry per metro with its tiles. Each tile has
  the same keys as an entry of ``config/areas.json`` (slug, label, latitude,
  longitude, radius_meters) plus ``reach_meters`` and its cell count.
- ``config/metro-boundaries.geojson``: the boundary each metro was tiled
  from (grown 150 m, simplified, holes filled). GitHub renders it as a map,
  and the coverage test re-derives every cell from it.

    python scripts/build_metros.py \\
        --urban-areas build-cache/census/tl_2024_us_uac20.zip \\
        --population build-cache/census/ua_population.json --top 50
    python scripts/build_metros.py --check   # re-verify the committed config

Reading the shapefile needs geopandas, which the cluster and Vercel installs
(requirements.txt) leave out. Use the full development install:
``pip install -e '.[dev]'``. ``--check`` needs only shapely.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import numpy as np
import shapely
from shapely.geometry import MultiPolygon, Polygon, mapping, shape

from runline.grid import DEFAULT_GRID_METERS
from runline.tiling import (
    DEFAULT_CORE_METERS,
    DEFAULT_MAX_MILES,
    metro_cells,
    metro_slug,
    needed_radius_meters,
    plan_tiles,
    tile_radius_meters,
    uncovered_cells,
)

METROS_PATH = ROOT / "config" / "metros.json"
BOUNDARIES_PATH = ROOT / "config" / "metro-boundaries.geojson"
FORMAT_VERSION = 1
# The Geofabrik US extract covers the 50 states and DC; Puerto Rico and the
# other territories are separate downloads.
STATES = set(
    "AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV "
    "NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY".split()
)
GROW_METERS = 150.0
SIMPLIFY_METERS = 100.0


# --- population -------------------------------------------------------------

def _normal_name(name: str) -> str:
    """Join key for names across Census products.

    TIGER says 'Charlottesville, VA', the Data API 'Charlottesville, VA Urban
    Area', and some tables use an en dash where TIGER uses '--'.
    """

    text = name.strip().lower().replace("–", "--").replace("—", "--")
    text = re.sub(r"\s+urban area(\s*\(2020\))?$", "", text)
    return re.sub(r"\s+", " ", text)


def _number(text) -> int:
    return int(float(str(text).replace(",", "").strip()))


def read_population(path: Path) -> tuple[dict[str, int], dict[str, int]]:
    """(by 5-digit code, by normalised name) from any supported population file."""

    by_code: dict[str, int] = {}
    by_name: dict[str, int] = {}
    raw = path.read_text(encoding="utf-8", errors="replace")
    suffix = path.suffix.lower()

    if suffix == ".json":
        rows = json.loads(raw)
        header = [str(column) for column in rows[0]]
        population = _pick(header, ("P1_001N", "POP20", "POPULATION", "P001001"))
        code = _pick(header, ("URBAN AREA", "UACE20", "GEOID20", "UACE", "GEOID"), required=False)
        name = _pick(header, ("NAME", "NAME20"), required=False)
        for row in rows[1:]:
            value = _number(row[population])
            if code is not None:
                by_code[str(row[code]).zfill(5)] = value
            if name is not None:
                by_name[_normal_name(str(row[name]))] = value
    elif suffix == ".csv":
        reader = csv.reader(raw.splitlines())
        header = next(reader)
        population = _pick(header, ("P1_001N", "POP20", "POPULATION", "POP"))
        code = _pick(header, ("UACE20", "GEOID20", "URBAN AREA", "UACE", "GEOID"), required=False)
        name = _pick(header, ("NAME20", "NAME", "URBAN AREA NAME"), required=False)
        if code is None and name is None:
            raise SystemExit(f"{path}: needs a GEOID/UACE or NAME column")
        for row in reader:
            if not row or not row[population].strip():
                continue
            value = _number(row[population])
            if code is not None:
                by_code[re.sub(r"\D", "", row[code])[-5:].zfill(5)] = value
            if name is not None:
                by_name[_normal_name(row[name])] = value
    else:
        # Federal Register list: 'Name.......   population   housing   land area'
        line = re.compile(r"^\s*(.+?)\.{2,}\s+([\d,]+)\s+([\d,]+)\s+([\d.,]+)\s*$")
        for text in raw.splitlines():
            match = line.match(text)
            if match:
                by_name[_normal_name(match.group(1))] = _number(match.group(2))
    if not by_code and not by_name:
        raise SystemExit(f"{path}: found no populations")
    return by_code, by_name


def _pick(header, choices, required=True):
    upper = [column.strip().upper() for column in header]
    for choice in choices:
        if choice in upper:
            return upper.index(choice)
    if required:
        raise SystemExit(f"population file has none of the columns {choices}; header is {header}")
    return None


# --- boundaries -------------------------------------------------------------

def read_urban_areas(path: Path):
    """(code, name, geometry in lon/lat) for every urban area in the shapefile."""

    import geopandas

    frame = geopandas.read_file(path)
    if frame.crs is not None:
        frame = frame.to_crs(4326)
    columns = {column.upper(): column for column in frame.columns}

    def column(prefix):
        for key, original in columns.items():
            if re.fullmatch(prefix + r"\d{0,2}", key):
                return original
        raise SystemExit(f"{path}: no {prefix} column in {list(frame.columns)}")

    code_column = next(
        (columns[key] for key in columns if re.fullmatch(r"(UACE|GEOID)\d{0,2}", key)), None
    )
    if code_column is None:
        raise SystemExit(f"{path}: no UACE/GEOID column in {list(frame.columns)}")
    name_column = column("NAME")
    return [
        (str(row[code_column]).zfill(5), str(row[name_column]), row.geometry)
        for _, row in frame.iterrows()
        if row.geometry is not None and not row.geometry.is_empty
    ]


def _states(name: str) -> list[str]:
    return name.rpartition(",")[2].strip().split("--")


def stored_boundary(geometry):
    """The boundary tiles are planned on: holes filled, grown, simplified, rounded.

    Growing by more than the simplification tolerance keeps the result a
    superset of the Census polygon, so no Census cell can fall outside it.
    Filling holes keeps big interior parks and forests, which Census leaves
    out of the urban area but runners very much start from.
    """

    parts = list(geometry.geoms) if hasattr(geometry, "geoms") else [geometry]
    filled = shapely.union_all([Polygon(part.exterior) for part in parts if not part.is_empty])
    latitude = filled.centroid.y
    grow = GROW_METERS / (111_320.0 * math.cos(math.radians(latitude)))
    tolerance = SIMPLIFY_METERS / 111_320.0
    grown = shapely.buffer(filled, grow, quad_segs=4)
    simplified = shapely.simplify(grown, tolerance, preserve_topology=True)
    rounded = shapely.transform(simplified, lambda xy: np.round(xy, 5))
    parts = list(rounded.geoms) if hasattr(rounded, "geoms") else [rounded]
    result = MultiPolygon([Polygon(part.exterior) for part in parts if part.area > 0])
    return shapely.make_valid(result)


def select_metros(urban_areas, by_code, by_name, top: int, include_territories: bool):
    ranked = []
    unmatched = 0
    for code, name, geometry in urban_areas:
        population = by_code.get(code, by_name.get(_normal_name(name)))
        if population is None:
            unmatched += 1
            continue
        if not include_territories and _states(name)[0] not in STATES:
            continue
        ranked.append((population, code, name, geometry))
    if unmatched:
        print(f"note: {unmatched} urban areas had no population row and were skipped")
    ranked.sort(key=lambda item: (-item[0], item[2]))
    return ranked[:top]


# --- output -----------------------------------------------------------------

def build(arguments) -> int:
    started = time.time()
    by_code, by_name = read_population(arguments.population)
    urban_areas = read_urban_areas(arguments.urban_areas)
    chosen = select_metros(urban_areas, by_code, by_name, arguments.top, arguments.include_territories)
    if not chosen:
        print("no urban areas matched the population file; check that both are 2020 urban areas")
        return 1

    needed = needed_radius_meters(arguments.max_miles)
    radius = tile_radius_meters(arguments.max_miles, arguments.core_meters, arguments.grid_meters)
    metros, features = [], []
    seen_slugs: set[str] = set()
    all_cells: set[str] = set()
    for rank, (population, code, name, geometry) in enumerate(chosen, 1):
        slug = metro_slug(name)
        if slug in seen_slugs:
            slug = f"{slug}-{code}"
        seen_slugs.add(slug)
        boundary = stored_boundary(geometry)
        # Plan on the boundary exactly as it will be read back from GeoJSON,
        # so the coverage test reproduces the same cells.
        boundary = shape(json.loads(json.dumps(mapping(boundary))))
        plan = plan_tiles(
            boundary, slug,
            max_miles=arguments.max_miles, core_meters=arguments.core_meters, grid_meters=arguments.grid_meters,
        )
        missing = uncovered_cells(plan.cells, plan.tiles, needed)
        if missing:
            raise SystemExit(f"{slug}: {len(missing)} cells uncovered, e.g. {missing[:3]}")
        counts = np.bincount(plan.owner, minlength=len(plan.tiles))
        all_cells.update(plan.cells.ids())
        metros.append(
            {
                "slug": slug,
                "name": name,
                "geoid": code,
                "population": population,
                "rank": rank,
                "cells": len(plan.cells),
                "tiles": [tile.to_json(slug, f"{name} ({position + 1}/{len(plan.tiles)})", int(counts[position]))
                          for position, tile in enumerate(plan.tiles)],
            }
        )
        features.append(
            {"type": "Feature", "properties": {"slug": slug, "name": name, "population": population},
             "geometry": mapping(boundary)}
        )
        print(f"{rank:3d}. {slug:32s} pop {population:>10,}  {len(plan.cells):>7,} cells  "
              f"{len(plan.tiles):>4} tiles  (patched {plan.patched})", flush=True)

    total_tiles = sum(len(metro["tiles"]) for metro in metros)
    total_cells = sum(metro["cells"] for metro in metros)
    payload = {
        "version": FORMAT_VERSION,
        "generated_by": "scripts/build_metros.py",
        "sources": {
            "urban_areas": arguments.urban_areas.name,
            "population": arguments.population.name,
        },
        "grid_meters": arguments.grid_meters,
        "max_miles": arguments.max_miles,
        "needed_radius_meters": round(needed, 1),
        "core_meters": arguments.core_meters,
        "tile_radius_meters": radius,
        "totals": {"metros": len(metros), "tiles": total_tiles, "cells": total_cells,
                   "unique_cells": len(all_cells)},
        "metros": metros,
    }
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(json.dumps(payload, indent=1) + "\n", encoding="utf-8")
    arguments.boundaries.write_text(
        json.dumps({"type": "FeatureCollection", "features": features}, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    print(f"\n{len(metros)} metros, {total_tiles} tiles of {radius / 1000:.1f} km, "
          f"{len(all_cells):,} unique cells ({total_cells - len(all_cells):,} shared by two metros) "
          f"in {time.time() - started:.0f}s")
    print(f"wrote {arguments.output} ({arguments.output.stat().st_size / 1024:.0f}KB) and "
          f"{arguments.boundaries} ({arguments.boundaries.stat().st_size / 1024:.0f}KB)")
    return 0


def check(metros_path: Path, boundaries_path: Path) -> list[str]:
    """Problems with a committed config; empty when every cell is covered.

    Used by --check and by tests/test_tiling.py. The needed radius comes from
    the planner's graph_radius_meters, not the file, so a planner change that
    grows the needed disc fails this until the tiles are regenerated.
    """

    config = json.loads(metros_path.read_text(encoding="utf-8"))
    boundaries = {
        feature["properties"]["slug"]: shape(feature["geometry"])
        for feature in json.loads(boundaries_path.read_text(encoding="utf-8"))["features"]
    }
    needed = needed_radius_meters(config["max_miles"])
    problems = []
    from runline.tiling import Tile

    for metro in config["metros"]:
        boundary = boundaries.get(metro["slug"])
        if boundary is None:
            problems.append(f"{metro['slug']}: no boundary")
            continue
        tiles = [Tile(t["slug"], t["latitude"], t["longitude"], t["radius_meters"], t["reach_meters"])
                 for t in metro["tiles"]]
        cells = metro_cells(boundary, config["grid_meters"])
        missing = uncovered_cells(cells, tiles, needed)
        if missing:
            problems.append(f"{metro['slug']}: {len(missing)} of {len(cells)} cells uncovered, e.g. {missing[:3]}")
        if len(cells) != metro["cells"]:
            problems.append(f"{metro['slug']}: {len(cells)} cells now, {metro['cells']} when generated")
    return problems


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="build_metros", description=__doc__.split("\n\n")[0])
    parser.add_argument("--urban-areas", type=Path, help="TIGER/Line tl_<year>_us_uac20.zip")
    parser.add_argument("--population", type=Path, help="urban-area populations (.json API, .csv, or .txt list)")
    parser.add_argument("--top", type=int, default=50)
    parser.add_argument("--max-miles", type=float, default=DEFAULT_MAX_MILES,
                        help="longest loop a tile must hold from every cell it owns")
    parser.add_argument("--core-meters", type=float, default=DEFAULT_CORE_METERS,
                        help="extra tile radius beyond what the longest loop needs")
    parser.add_argument("--grid-meters", type=float, default=DEFAULT_GRID_METERS)
    parser.add_argument("--include-territories", action="store_true",
                        help="keep Puerto Rico and other areas outside the Geofabrik US extract")
    parser.add_argument("--output", type=Path, default=METROS_PATH)
    parser.add_argument("--boundaries", type=Path, default=BOUNDARIES_PATH)
    parser.add_argument("--check", action="store_true", help="verify the committed config and exit")
    arguments = parser.parse_args(argv)

    if arguments.check:
        problems = check(arguments.output, arguments.boundaries)
        for problem in problems:
            print(problem)
        print("ok: every cell is covered" if not problems else f"{len(problems)} problem(s)")
        return 1 if problems else 0
    if arguments.urban_areas is None or arguments.population is None:
        parser.error("--urban-areas and --population are required (or use --check)")
    return build(arguments)


if __name__ == "__main__":
    raise SystemExit(main())
