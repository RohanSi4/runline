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
# DEM tiles live here, outside graphs/ so they are not shipped in the function
# bundle. They are about 5MB per one-degree tile and are fetched on demand.
ELEVATION_CACHE_DIR = PROJECT_ROOT / "build-cache"

# Measured on a full 5 mile request: 57k nodes peaks near 490MB RSS, 118k nodes
# peaks near 1186MB and would OOM a 1GB function. Roughly 8MB of resident memory
# per 1000 nodes, so 70k leaves headroom under a 1GB budget. Dense cities need a
# smaller radius, not a bigger memory budget.
NODE_WARNING_THRESHOLD = 70_000


def bake_elevation(slug: str, graph) -> None:
    """Attach Copernicus GLO-90 elevation from tiles cached in build-cache/dem.

    Open-Meteo serves the same DEM but rate-limits bulk backfills, which is
    why the shipped Charlottesville graph was never baked. Reading the public
    tiles directly has no limit and takes seconds per area.
    """

    from runline.elevation import enrich_graph_copernicus

    baked = enrich_graph_copernicus(graph, ELEVATION_CACHE_DIR / "dem")
    total = graph.number_of_nodes()
    # The planner refuses to rank on elevation below 95% coverage, so only a
    # near-complete bake may skip the per-request fallback.
    graph.graph["elevation_baked"] = baked >= 0.95 * total
    print(f"  {slug}: baked elevation for {baked} of {total} nodes")


def build_place_index(slug: str, graph, start_entries: list) -> None:
    """Write the place list that powers address autocomplete.

    Nominatim's usage policy forbids per-keystroke autocomplete, and the app
    only covers one city, so suggestions come from the shipped map instead:
    every named street plus the named public start points. Selecting one gives
    coordinates directly, so a chosen suggestion skips geocoding entirely, and
    nothing outside the mapped area can be suggested.
    """

    sums: dict[str, list[float]] = {}
    for _, target, _, data in graph.edges(keys=True, data=True):
        raw = data.get("name")
        names = [raw] if isinstance(raw, str) else raw if isinstance(raw, list) else []
        node = graph.nodes[target]
        for name in names:
            if not isinstance(name, str) or not name.strip():
                continue
            entry = sums.setdefault(name, [0.0, 0.0, 0.0])
            entry[0] += float(node["y"])
            entry[1] += float(node["x"])
            entry[2] += 1

    places = [
        {
            "name": name,
            "kind": "street",
            "latitude": round(total_lat / count, 6),
            "longitude": round(total_lon / count, 6),
        }
        for name, (total_lat, total_lon, count) in sums.items()
        if count
    ]

    generic = {"Parking", "Park", "Trailhead"}
    seen = {place["name"] for place in places}
    for entry in start_entries:
        if entry["label"] in generic or entry["label"] in seen:
            continue
        seen.add(entry["label"])
        places.append(
            {
                "name": entry["label"],
                "kind": entry["kind"],
                "latitude": round(entry["latitude"], 6),
                "longitude": round(entry["longitude"], 6),
            }
        )

    # Drop names that read as map annotations rather than places a runner
    # would type, e.g. a trail blaze colour like "(yellow blaze)".
    places = [
        place
        for place in places
        if len(place["name"]) >= 3 and place["name"][:1].isalnum()
    ]
    places.sort(key=lambda place: place["name"])
    path = GRAPHS_DIR / f"{slug}-places.json"
    path.write_text(json.dumps(places, separators=(",", ":")) + "\n", encoding="utf-8")
    print(f"  {slug}: {len(places)} autocomplete places ({path.stat().st_size / 1024:.0f}KB)")


def build_start_candidates(ox, slug: str, latitude: float, longitude: float, radius: float) -> list:
    """Precompute the drive-radius start points and the road graph they need.

    discover_public_starts otherwise makes two Overpass calls per request, one
    for trailhead/parking/park features and one for the drive network. Neither
    can complete inside a serverless request, so both are shipped.
    """

    sys.path.insert(0, str(PROJECT_ROOT / "src"))
    from runline.starts import _feature_kind, BLOCKED_ACCESS, _text_values

    features = ox.features.features_from_point(
        (latitude, longitude),
        tags={"information": "trailhead", "amenity": "parking", "leisure": "park"},
        dist=radius,
    )
    entries = []
    for _, row in features.iterrows():
        data = row.to_dict()
        kind = _feature_kind(data)
        if kind is None or _text_values(data.get("access")) & BLOCKED_ACCESS:
            continue
        geometry = row.geometry
        point = (
            geometry
            if geometry.geom_type == "Point"
            else geometry.representative_point()
        )
        name = data.get("name")
        entries.append(
            {
                "label": str(name)
                if isinstance(name, str) and name.strip()
                else kind.title(),
                "kind": kind,
                "latitude": float(point.y),
                "longitude": float(point.x),
            }
        )
    (GRAPHS_DIR / f"{slug}-starts.json").write_text(
        json.dumps(entries, indent=1) + "\n", encoding="utf-8"
    )

    drive = ox.graph.graph_from_point(
        (latitude, longitude),
        dist=radius + 1_000,
        network_type="drive",
        simplify=True,
        retain_all=False,
    )
    # retain_all=False keeps the largest WEAKLY connected component, but drive
    # distance is a directed shortest path. Without this the graph had 19
    # strongly connected components and every candidate was unreachable.
    from runline.osm import _routable_core

    drive = _routable_core(drive)
    drive_payload = gzip.compress(pickle.dumps(drive, protocol=5), 6, mtime=0)
    (GRAPHS_DIR / f"{slug}-drive.pkl.gz").write_bytes(drive_payload)
    print(
        f"  {slug}: {len(entries)} start candidates, "
        f"drive graph {drive.number_of_nodes()} nodes "
        f"({len(drive_payload) / 1048576:.1f}MB gz)"
    )
    return entries


def build_area(area: dict, *, force: bool, known: dict | None = None) -> dict:
    import osmnx as ox

    slug = area["slug"]
    radius = float(area["radius_meters"])
    destination = GRAPHS_DIR / f"{slug}.pkl.gz"

    if destination.exists() and not force and known:
        # Coordinates come from the manifest; the config only carries a query.
        print(f"  {slug}: already built, skipping (use --force to rebuild)")
        latitude, longitude = known["latitude"], known["longitude"]
    else:
        started = time.time()
        if "latitude" in area and "longitude" in area:
            latitude, longitude = float(area["latitude"]), float(area["longitude"])
        else:
            latitude, longitude = ox.geocoder.geocode(area["query"])
            latitude, longitude = float(latitude), float(longitude)

        # What ox.graph.graph_from_point(..., network_type="walk",
        # simplify=True, retain_all=True) builds, plus the signal tags and
        # nodes it would drop (runline.pbf.walk_graph_from_responses).
        from osmnx import _overpass
        from runline.pbf import tile_polygons, walk_graph_from_responses

        polygon, buffered = tile_polygons(latitude, longitude, radius)
        graph = walk_graph_from_responses(
            _overpass._download_overpass_network(buffered, "walk", None), polygon, buffered
        )
        # Bake terrain elevation into the graph. The per-request Open-Meteo
        # cache lives in /tmp on a serverless host and is wiped between
        # instances, so an unbaked graph refetches on every request.
        bake_elevation(slug, graph)

        # mtime=0: pools are tied to this file's sha256, and gzip's header
        # would otherwise change it on every rebuild.
        payload = gzip.compress(pickle.dumps(graph, protocol=5), 6, mtime=0)
        GRAPHS_DIR.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(payload)

        start_entries = build_start_candidates(ox, slug, latitude, longitude, radius)
        build_place_index(slug, graph, start_entries)

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
        "starts_file": f"{slug}-starts.json",
        "drive_file": f"{slug}-drive.pkl.gz",
        "places_file": f"{slug}-places.json",
    }


def main() -> int:
    parser = argparse.ArgumentParser(prog="build_graphs")
    parser.add_argument("--force", action="store_true", help="rebuild existing graphs")
    parser.add_argument("--only", help="build a single area by slug")
    parser.add_argument(
        "--bake-only",
        action="store_true",
        help="bake elevation into already built graphs without downloading them again",
    )
    parser.add_argument(
        "--starts-only",
        action="store_true",
        help="rebuild only the start candidates and drive graph",
    )
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

    manifest_path = GRAPHS_DIR / "manifest.json"
    existing = {}
    if manifest_path.exists():
        existing = {
            entry["slug"]: entry
            for entry in json.loads(manifest_path.read_text(encoding="utf-8"))["areas"]
        }

    if arguments.bake_only:
        for area in selected:
            path = GRAPHS_DIR / existing[area["slug"]]["file"]
            with gzip.open(path, "rb") as handle:
                graph = pickle.load(handle)
            bake_elevation(area["slug"], graph)
            path.write_bytes(gzip.compress(pickle.dumps(graph, protocol=5), 6, mtime=0))
        entries = [existing[area["slug"]] for area in selected]
    elif arguments.starts_only:
        import osmnx as ox

        for area in selected:
            known = existing[area["slug"]]
            start_entries = build_start_candidates(
                ox,
                area["slug"],
                known["latitude"],
                known["longitude"],
                float(area["radius_meters"]),
            )
            with gzip.open(GRAPHS_DIR / known["file"], "rb") as handle:
                build_place_index(area["slug"], pickle.load(handle), start_entries)
        entries = [
            {**existing[area["slug"]],
             "starts_file": f"{area['slug']}-starts.json",
             "drive_file": f"{area['slug']}-drive.pkl.gz",
             "places_file": f"{area['slug']}-places.json"}
            for area in selected
        ]
    else:
        print(f"building {len(selected)} area(s) into {GRAPHS_DIR}")
        entries = [
            build_area(area, force=arguments.force, known=existing.get(area["slug"]))
            for area in selected
        ]
    for entry in entries:
        existing[entry["slug"]] = entry

    # Drop entries whose graph is gone, otherwise removing an area from the
    # config leaves the app advertising coverage it cannot serve.
    configured = {area["slug"] for area in config["areas"]}
    ordered = sorted(
        (
            entry
            for entry in existing.values()
            if entry["slug"] in configured and (GRAPHS_DIR / entry["file"]).exists()
        ),
        key=lambda entry: entry["label"],
    )
    manifest_path.write_text(
        json.dumps({"version": 1, "areas": ordered}, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {manifest_path} with {len(ordered)} area(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
