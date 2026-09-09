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
# Kept outside graphs/ so it is not shipped in the function bundle, but
# committed so a rebuild does not refetch tens of thousands of cells.
ELEVATION_CACHE_DIR = PROJECT_ROOT / "build-cache"

# Measured on a full 5 mile request: 57k nodes peaks near 490MB RSS, 118k nodes
# peaks near 1186MB and would OOM a 1GB function. Roughly 8MB of resident memory
# per 1000 nodes, so 70k leaves headroom under a 1GB budget. Dense cities need a
# smaller radius, not a bigger memory budget.
NODE_WARNING_THRESHOLD = 70_000


class ThrottledSession:
    """Polite Open-Meteo client for bulk backfills.

    Baking a whole area asks for tens of thousands of elevation cells in one
    run, which trips Open-Meteo's rate limit and returns 429. Requests are
    spaced out and retried with exponential backoff. This is build-time only;
    the runtime path never calls the service.
    """

    def __init__(self, delay: float = 2.5, attempts: int = 8) -> None:
        self.delay = delay
        self.attempts = attempts

    def get(self, url: str, **kwargs):
        import requests

        for attempt in range(self.attempts):
            time.sleep(self.delay)
            response = requests.get(url, **kwargs)
            if response.status_code != 429:
                return response
            backoff = min(60.0, self.delay * (2**attempt))
            print(f"    rate limited, waiting {backoff:.0f}s")
            time.sleep(backoff)
        return response


def nodes_with_elevation(graph) -> int:
    return sum(1 for _, data in graph.nodes(data=True) if "elevation" in data)


def build_start_candidates(ox, slug: str, latitude: float, longitude: float, radius: float) -> None:
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
    drive_payload = gzip.compress(pickle.dumps(drive, protocol=5), 6)
    (GRAPHS_DIR / f"{slug}-drive.pkl.gz").write_bytes(drive_payload)
    print(
        f"  {slug}: {len(entries)} start candidates, "
        f"drive graph {drive.number_of_nodes()} nodes "
        f"({len(drive_payload) / 1048576:.1f}MB gz)"
    )


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

        graph = ox.graph.graph_from_point(
            (latitude, longitude),
            dist=radius,
            network_type="walk",
            simplify=True,
            retain_all=True,
        )
        # Bake terrain elevation into the graph. Open-Meteo is cached per ~110m
        # cell, but that cache lives in /tmp on a serverless host and is wiped
        # between instances, so an unbaked graph refetches on every request.
        from runline.elevation import enrich_graph_open_meteo

        ELEVATION_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        elevation_cache = ELEVATION_CACHE_DIR / f"elevation-{slug}.json"
        try:
            fetched = enrich_graph_open_meteo(
                graph, elevation_cache, session=ThrottledSession()
            )
            graph.graph["elevation_baked"] = True
            print(
                f"  {slug}: baked elevation for {nodes_with_elevation(graph)} nodes "
                f"({fetched} cells fetched)"
            )
        except Exception as error:
            # Open-Meteo rate-limits large backfills. A graph without baked
            # elevation still works; the app falls back to fetching per request.
            print(f"  {slug}: elevation NOT baked ({error.__class__.__name__}). "
                  "Rerun --force later to bake it; the cell cache resumes.")

        payload = gzip.compress(pickle.dumps(graph, protocol=5), 6)
        GRAPHS_DIR.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(payload)

        build_start_candidates(ox, slug, latitude, longitude, radius)

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
    }


def main() -> int:
    parser = argparse.ArgumentParser(prog="build_graphs")
    parser.add_argument("--force", action="store_true", help="rebuild existing graphs")
    parser.add_argument("--only", help="build a single area by slug")
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

    if arguments.starts_only:
        import osmnx as ox

        for area in selected:
            known = existing[area["slug"]]
            build_start_candidates(
                ox,
                area["slug"],
                known["latitude"],
                known["longitude"],
                float(area["radius_meters"]),
            )
        entries = [
            {**existing[area["slug"]],
             "starts_file": f"{area['slug']}-starts.json",
             "drive_file": f"{area['slug']}-drive.pkl.gz"}
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
