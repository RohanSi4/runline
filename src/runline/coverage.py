"""Precomputed street-graph coverage.

Runline ships a small set of prebuilt OSM walk graphs so that request-time
route generation never has to call the Overpass API. Downloading a graph takes
between ten seconds and several minutes depending on how heavily Overpass is
throttling, which is far longer than a serverless function may run, so on
Vercel the download path is disabled entirely and unsupported locations return
a clear error instead.

Graphs are stored as gzipped pickles rather than GraphML: for a 40k-node graph
that is a 0.9s load with a 145MB peak, against 6.3s and 737MB for GraphML.
The pickles are build artifacts committed alongside the code, so they are
trusted input; never point RUNLINE_GRAPHS_DIR at a directory you do not own.
"""

from __future__ import annotations

import gzip
import json
import os
import pickle
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import networkx as nx

from .geo import distance_meters
from .models import Coordinate

MANIFEST_NAME = "manifest.json"


class CoverageError(RuntimeError):
    """No precomputed graph covers the requested location."""


@dataclass(frozen=True)
class Area:
    slug: str
    label: str
    center: Coordinate
    radius_meters: float
    filename: str
    starts_filename: str | None = None
    drive_filename: str | None = None
    places_filename: str | None = None

    def covers(self, origin: Coordinate, radius_meters: float) -> bool:
        """True when a disc of ``radius_meters`` around origin fits inside this area."""

        return (
            distance_meters(self.center, origin) + radius_meters
            <= self.radius_meters
        )


def graphs_root() -> Path:
    override = os.environ.get("RUNLINE_GRAPHS_DIR")
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[2] / "graphs"


def downloads_allowed() -> bool:
    """Live Overpass downloads are fine locally but never on a serverless host."""

    override = os.environ.get("RUNLINE_ALLOW_OSM_DOWNLOAD")
    if override is not None:
        return override.strip().lower() not in {"", "0", "false", "no"}
    return not os.environ.get("VERCEL")


@lru_cache(maxsize=4)
def _read_manifest(root: str) -> tuple[Area, ...]:
    path = Path(root) / MANIFEST_NAME
    if not path.exists():
        return ()
    payload = json.loads(path.read_text(encoding="utf-8"))
    return tuple(
        Area(
            slug=entry["slug"],
            label=entry["label"],
            center=Coordinate(float(entry["latitude"]), float(entry["longitude"])),
            radius_meters=float(entry["radius_meters"]),
            filename=entry["file"],
            starts_filename=entry.get("starts_file"),
            drive_filename=entry.get("drive_file"),
            places_filename=entry.get("places_file"),
        )
        for entry in payload.get("areas", ())
    )


def areas() -> tuple[Area, ...]:
    return _read_manifest(str(graphs_root()))


def area_labels() -> tuple[str, ...]:
    return tuple(area.label for area in areas())


def find_area(origin: Coordinate, radius_meters: float) -> Area | None:
    """Smallest-distance area whose disc fully contains the requested one."""

    covering = [area for area in areas() if area.covers(origin, radius_meters)]
    if not covering:
        return None
    return min(covering, key=lambda area: distance_meters(area.center, origin))


# Holds one area's walk graph plus its drive graph; at maxsize=1 the two would
# evict each other on every request that discovers starts.
@lru_cache(maxsize=2)
def _load_graph_file(path: str) -> nx.MultiDiGraph:
    with gzip.open(path, "rb") as handle:
        return pickle.load(handle)


def load_area_graph(area: Area) -> nx.MultiDiGraph:
    """Load and memoize an area's graph.

    A warm instance answers repeat requests for the same area without touching
    disk.
    """

    path = graphs_root() / area.filename
    if not path.exists():
        raise CoverageError(
            f"Map data for {area.label} is missing from this deployment."
        )
    return _load_graph_file(str(path))


def containing_area(origin: Coordinate) -> Area | None:
    """The area a point sits inside, ignoring how much map a route would need."""

    inside = [
        area
        for area in areas()
        if distance_meters(area.center, origin) <= area.radius_meters
    ]
    if not inside:
        return None
    return min(inside, key=lambda area: distance_meters(area.center, origin))


@lru_cache(maxsize=4)
def _load_json_file(path: str) -> tuple:
    return tuple(json.loads(Path(path).read_text(encoding="utf-8")))


def load_area_starts(area: Area) -> tuple:
    """Public start candidates precomputed for an area, or () if none shipped."""

    if not area.starts_filename:
        return ()
    path = graphs_root() / area.starts_filename
    if not path.exists():
        return ()
    return _load_json_file(str(path))


def load_area_places(area: Area) -> tuple:
    """Named streets and public places used for address autocomplete."""

    if not area.places_filename:
        return ()
    path = graphs_root() / area.places_filename
    if not path.exists():
        return ()
    return _load_json_file(str(path))


def load_area_drive_graph(area: Area) -> nx.MultiDiGraph | None:
    """Road network used to measure drive distance to a discovered start."""

    if not area.drive_filename:
        return None
    path = graphs_root() / area.drive_filename
    if not path.exists():
        return None
    return _load_graph_file(str(path))


def unsupported_message(origin: Coordinate, radius_meters: float) -> str:
    """Explain a coverage miss, distinguishing a bad location from a long route.

    A start point inside a shipped area that still fails to match means the
    requested distance needs more map than that area carries. Anywhere else is
    simply outside coverage, which is a different thing to tell the runner.
    """

    available = areas()
    if not available:
        return "No map data is available in this deployment yet."

    joined = ", ".join(area.label for area in available)
    containing = [
        area
        for area in available
        if distance_meters(area.center, origin) <= area.radius_meters
    ]
    if containing:
        nearest = min(
            containing, key=lambda area: distance_meters(area.center, origin)
        )
        return (
            f"That route is too long for the {nearest.label} map Runline ships. "
            "Try a shorter distance or a start point closer to the centre."
        )
    return f"Runline has no map data for that location yet. Supported areas: {joined}."
