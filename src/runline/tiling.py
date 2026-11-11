"""Cut metros into overlapping graph tiles for the pool precompute.

A pool for a cell is generated on a graph that must hold every loop the
planner could return from that cell, which for the longest target is a disc of
``graph_radius_meters`` around the cell's origin node (10.6 km at 12 miles).
A dense metro is far too big for one graph, so it is covered by tiles: discs
of ``radius_meters`` whose centres sit on a hex lattice.

Each tile owns the cells whose centre is within its *reach* of the tile
centre, where::

    reach = radius - needed_radius - cell_half_diagonal

so any origin node in an owned cell has its whole needed disc inside the tile.
A cell may be within reach of several tiles; it is assigned to the nearest
one (``assign_cells``), which gives every cell exactly one tile and is the
grouping the precompute uses to give each SLURM task one tile's cells.

Everything here works in degrees with haversine checks, so the guarantee does
not depend on a map projection: the lattice is laid out approximately, then
every cell is verified and any gap gets an extra tile.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

import numpy as np
import shapely
from scipy.spatial import cKDTree

from .geo import EARTH_RADIUS_METERS
from .grid import (
    METERS_PER_DEGREE_LATITUDE,
    METERS_PER_DEGREE_LONGITUDE,
    cell_half_diagonal_meters,
    column_width_degrees,
    row_centre_latitude,
    row_height_degrees,
)

DEFAULT_MAX_MILES = 12.0
# Width of the band of cells a tile owns beyond what its longest loop needs.
# Tile graph size is set mostly by the needed radius (10.6 km), so a wider
# core adds little per tile but cuts the tile count by its square:
# 3 km gives tiles of about 13.8 km radius.
DEFAULT_CORE_METERS = 3_000.0
# Lattice spacing is shrunk by this much so approximate degree conversions
# across a large metro rarely leave a gap; any that remain are patched.
LATTICE_SAFETY = 0.97


@dataclass(frozen=True)
class Tile:
    slug: str
    latitude: float
    longitude: float
    radius_meters: float
    reach_meters: float

    def to_json(self, metro: str, label: str, cells: int) -> dict:
        # Same keys as an entry of config/areas.json, so a graph builder can
        # treat a tile exactly like today's areas.
        return {
            "slug": self.slug,
            "label": label,
            "metro": metro,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "radius_meters": self.radius_meters,
            "reach_meters": self.reach_meters,
            "cells": cells,
        }


@dataclass
class Cells:
    """Cell centres of one metro on the global grid, as parallel arrays."""

    rows: np.ndarray
    columns: np.ndarray
    latitudes: np.ndarray
    longitudes: np.ndarray
    grid_meters: float

    def __len__(self) -> int:
        return int(self.rows.size)

    def ids(self) -> list[str]:
        grid = int(self.grid_meters)
        return [f"{grid}:{row}:{column}" for row, column in zip(self.rows.tolist(), self.columns.tolist())]


@dataclass
class TilePlan:
    tiles: list[Tile]
    cells: Cells
    owner: np.ndarray  # index into tiles for each cell
    patched: int = 0
    extra: dict = field(default_factory=dict)


def needed_radius_meters(max_miles: float = DEFAULT_MAX_MILES) -> float:
    """Graph radius the planner crops to for the longest target served."""

    from .models import RoutePreferences
    from .osm import graph_radius_meters

    return graph_radius_meters(RoutePreferences(max_miles))


def tile_radius_meters(
    max_miles: float = DEFAULT_MAX_MILES,
    core_meters: float = DEFAULT_CORE_METERS,
    grid_meters: float = 200.0,
) -> float:
    """Tile disc radius, rounded up to 100 m so configs read cleanly."""

    raw = needed_radius_meters(max_miles) + cell_half_diagonal_meters(grid_meters) + core_meters
    return math.ceil(raw / 100.0) * 100.0


def reach_meters(radius_meters: float, max_miles: float, grid_meters: float) -> float:
    return radius_meters - needed_radius_meters(max_miles) - cell_half_diagonal_meters(grid_meters)


def haversine_meters(latitude, longitude, latitudes, longitudes) -> np.ndarray:
    """Distance from one point to many, matching runline.geo.distance_meters."""

    first = math.radians(latitude)
    second = np.radians(latitudes)
    delta_latitude = second - first
    delta_longitude = np.radians(np.asarray(longitudes) - longitude)
    value = (
        np.sin(delta_latitude / 2) ** 2
        + math.cos(first) * np.cos(second) * np.sin(delta_longitude / 2) ** 2
    )
    return 2 * EARTH_RADIUS_METERS * np.arcsin(np.sqrt(np.minimum(value, 1.0)))


def metro_cells(boundary, grid_meters: float = 200.0) -> Cells:
    """Every grid cell that touches the boundary (lon/lat polygon or multipolygon).

    A cell is kept when its centre lies inside the boundary grown by the cell
    half-diagonal. That is a slight superset of the cells that intersect the
    boundary, which errs toward covering too much rather than too little.
    """

    min_lon, min_lat, max_lon, max_lat = boundary.bounds
    half_diagonal = cell_half_diagonal_meters(grid_meters)
    widest = max(abs(min_lat), abs(max_lat))
    grow = half_diagonal / (METERS_PER_DEGREE_LONGITUDE * math.cos(math.radians(widest)))
    grown = shapely.buffer(boundary, grow, quad_segs=4)
    shapely.prepare(grown)
    min_lon, min_lat, max_lon, max_lat = grown.bounds

    height = row_height_degrees(grid_meters)
    first_row = math.floor(min_lat / height)
    last_row = math.floor(max_lat / height)
    rows, columns, latitudes, longitudes = [], [], [], []
    for row in range(first_row, last_row + 1):
        width = column_width_degrees(row, grid_meters)
        first_column = math.floor(min_lon / width)
        last_column = math.floor(max_lon / width)
        row_columns = np.arange(first_column, last_column + 1, dtype=np.int64)
        row_longitudes = (row_columns + 0.5) * width
        row_latitude = row_centre_latitude(row, grid_meters)
        inside = shapely.contains_xy(grown, row_longitudes, np.full(row_longitudes.shape, row_latitude))
        if not inside.any():
            continue
        count = int(inside.sum())
        rows.append(np.full(count, row, dtype=np.int64))
        columns.append(row_columns[inside])
        latitudes.append(np.full(count, row_latitude))
        longitudes.append(row_longitudes[inside])

    def join(parts, dtype):
        return np.concatenate(parts) if parts else np.empty(0, dtype=dtype)

    return Cells(
        join(rows, np.int64),
        join(columns, np.int64),
        join(latitudes, float),
        join(longitudes, float),
        grid_meters,
    )


def hex_lattice(bounds, spacing_meters: float) -> list[tuple[float, float]]:
    """Points whose discs of radius spacing/sqrt(3) cover the bounds.

    Rows are laid out in latitude and each row's columns are converted to
    degrees at that row's own latitude, so the lattice stays close to regular
    even across a metro a degree or two tall.
    """

    min_lon, min_lat, max_lon, max_lat = bounds
    radius = spacing_meters / math.sqrt(3)
    row_step = 1.5 * radius / METERS_PER_DEGREE_LATITUDE
    start_lat = min_lat - radius / METERS_PER_DEGREE_LATITUDE
    stop_lat = max_lat + radius / METERS_PER_DEGREE_LATITUDE
    points = []
    index = 0
    latitude = start_lat
    while latitude <= stop_lat + 1e-12:
        column_step = spacing_meters / (METERS_PER_DEGREE_LONGITUDE * math.cos(math.radians(latitude)))
        offset = column_step / 2 if index % 2 else 0.0
        longitude = min_lon - column_step + offset
        while longitude <= max_lon + column_step:
            points.append((round(latitude, 6), round(longitude, 6)))
            longitude += column_step
        latitude += row_step
        index += 1
    return points


def _local_xy(latitudes, longitudes, reference_latitude: float) -> np.ndarray:
    scale = math.cos(math.radians(reference_latitude))
    return np.column_stack(
        (
            np.asarray(longitudes) * METERS_PER_DEGREE_LONGITUDE * scale,
            np.asarray(latitudes) * METERS_PER_DEGREE_LATITUDE,
        )
    )


def assign_cells(
    latitudes: np.ndarray,
    longitudes: np.ndarray,
    tile_latitudes: np.ndarray,
    tile_longitudes: np.ndarray,
    tile_reaches: np.ndarray,
    candidates: int = 8,
) -> np.ndarray:
    """Index of the owning tile for each cell centre, or -1 if none reaches it.

    The owner is the tile with the nearest centre among those whose reach
    contains the cell (haversine), ties to the lower index. A k-d tree in a
    local flat projection only narrows the candidates; the decision itself is
    made on haversine distances, then any cell the narrowing missed is checked
    against every tile so the result never depends on the projection.
    """

    count = len(latitudes)
    owner = np.full(count, -1, dtype=np.int64)
    if count == 0 or len(tile_latitudes) == 0:
        return owner

    reference = float(np.mean(latitudes))
    tree = cKDTree(_local_xy(tile_latitudes, tile_longitudes, reference))
    k = min(candidates, len(tile_latitudes))
    _, nearest = tree.query(_local_xy(latitudes, longitudes, reference), k=k)
    nearest = nearest.reshape(count, k)

    best = np.full(count, np.inf)
    for column in range(k):
        tile_index = nearest[:, column]
        distance = _pairwise_haversine(latitudes, longitudes, tile_latitudes[tile_index], tile_longitudes[tile_index])
        better = (distance <= tile_reaches[tile_index]) & (
            (distance < best) | ((distance == best) & (tile_index < owner))
        )
        owner[better] = tile_index[better]
        best[better] = distance[better]

    # Cells the k nearest centres did not reach: exhaustive check, so a cell
    # is only ever reported uncovered when no tile at all reaches it.
    missing = np.flatnonzero(owner < 0)
    for cell in missing:
        distance = haversine_meters(latitudes[cell], longitudes[cell], tile_latitudes, tile_longitudes)
        reached = np.flatnonzero(distance <= tile_reaches)
        if reached.size:
            owner[cell] = reached[np.argmin(distance[reached])]
    return owner


def _pairwise_haversine(lat1, lon1, lat2, lon2) -> np.ndarray:
    first = np.radians(lat1)
    second = np.radians(lat2)
    value = (
        np.sin((second - first) / 2) ** 2
        + np.cos(first) * np.cos(second) * np.sin(np.radians(np.asarray(lon2) - np.asarray(lon1)) / 2) ** 2
    )
    return 2 * EARTH_RADIUS_METERS * np.arcsin(np.sqrt(np.minimum(value, 1.0)))


def plan_tiles(
    boundary,
    slug: str,
    *,
    max_miles: float = DEFAULT_MAX_MILES,
    core_meters: float = DEFAULT_CORE_METERS,
    grid_meters: float = 200.0,
) -> TilePlan:
    """Tiles covering every cell of one metro, each cell owned by one tile."""

    cells = metro_cells(boundary, grid_meters)
    radius = tile_radius_meters(max_miles, core_meters, grid_meters)
    reach = reach_meters(radius, max_miles, grid_meters)
    if reach <= 0:
        raise ValueError("tile radius leaves no reach; increase core_meters")
    if len(cells) == 0:
        return TilePlan([], cells, np.empty(0, dtype=np.int64))

    lattice = hex_lattice(boundary.bounds, math.sqrt(3) * reach * LATTICE_SAFETY)
    centres = np.array(lattice, dtype=float).reshape(-1, 2)
    reaches = np.full(len(centres), reach)
    owner = assign_cells(cells.latitudes, cells.longitudes, centres[:, 0], centres[:, 1], reaches)

    # Patch gaps: a new tile on the first uncovered cell, until none remain.
    # With the lattice safety margin this almost never runs.
    patched = 0
    while (owner < 0).any():
        cell = int(np.flatnonzero(owner < 0)[0])
        centres = np.vstack([centres, [round(cells.latitudes[cell], 6), round(cells.longitudes[cell], 6)]])
        reaches = np.append(reaches, reach)
        owner = assign_cells(cells.latitudes, cells.longitudes, centres[:, 0], centres[:, 1], reaches)
        patched += 1

    # Drop lattice points that own no cell (water, the corners of the bounds).
    used = np.unique(owner)
    remap = np.full(len(centres), -1, dtype=np.int64)
    remap[used] = np.arange(used.size)
    tiles = [
        Tile(
            slug=f"{slug}-{position:03d}",
            latitude=float(centres[index, 0]),
            longitude=float(centres[index, 1]),
            radius_meters=radius,
            # Rounded down so a reader of the config never over-assigns.
            reach_meters=math.floor(reach * 10) / 10,
        )
        for position, index in enumerate(used.tolist())
    ]
    return TilePlan(tiles, cells, remap[owner], patched=patched)


def uncovered_cells(cells: Cells, tiles: list[Tile], needed_meters: float) -> list[str]:
    """Cells with no tile whose disc holds the cell's whole needed disc.

    This is the property the precompute relies on, checked directly rather
    than through ``reach``: for some tile, the distance from the tile centre
    to the cell centre plus the cell's half-diagonal plus the needed radius
    is at most the tile radius.
    """

    if len(cells) == 0:
        return []
    if not tiles:
        return cells.ids()
    latitudes = np.array([tile.latitude for tile in tiles])
    longitudes = np.array([tile.longitude for tile in tiles])
    radii = np.array([tile.radius_meters for tile in tiles])
    slack = cell_half_diagonal_meters(cells.grid_meters) + needed_meters
    owner = assign_cells(cells.latitudes, cells.longitudes, latitudes, longitudes, radii - slack)
    ids = cells.ids()
    return [ids[index] for index in np.flatnonzero(owner < 0).tolist()]


def metro_slug(name: str) -> str:
    """'New York--Jersey City--Newark, NY--NJ' -> 'new-york-ny'."""

    place, _, states = name.rpartition(",")
    if not place:
        place, states = name, ""
    first_place = place.split("--")[0]
    first_state = states.strip().split("--")[0]
    text = f"{first_place} {first_state}".lower()
    return re.sub(r"[^a-z0-9]+", "-", text).strip("-")


def metro_label(name: str) -> str:
    """'New York--Jersey City--Newark, NY--NJ' -> 'New York, NY', for the app."""

    place, _, states = name.rpartition(",")
    if not place:
        return name.split("--")[0].strip()
    return f"{place.split('--')[0].strip()}, {states.strip().split('--')[0]}"


def owned_cells(config: dict, boundaries: dict) -> dict[str, list[str]]:
    """Cell ids each tile of a metros config owns, over all metros at once.

    Adjacent metros' tiles overlap and some cells fall in two metros, so
    cells are pooled and deduplicated first, then every cell goes to the
    nearest tile that reaches it (``assign_cells``) among all tiles. Each cell
    is computed by exactly one tile, so a merged run has no duplicates.
    """

    grid_meters = config["grid_meters"]
    parts = [metro_cells(boundaries[metro["slug"]], grid_meters) for metro in config["metros"]]
    rows = np.concatenate([part.rows for part in parts])
    columns = np.concatenate([part.columns for part in parts])
    keys, first = np.unique(np.column_stack((rows, columns)), axis=0, return_index=True)
    latitudes = np.concatenate([part.latitudes for part in parts])[first]
    longitudes = np.concatenate([part.longitudes for part in parts])[first]

    tiles = [tile for metro in config["metros"] for tile in metro["tiles"]]
    owner = assign_cells(
        latitudes,
        longitudes,
        np.array([tile["latitude"] for tile in tiles]),
        np.array([tile["longitude"] for tile in tiles]),
        np.array([tile["reach_meters"] for tile in tiles]),
    )
    if (owner < 0).any():
        raise ValueError(f"{int((owner < 0).sum())} cells are not reached by any tile; regenerate the config")
    grid = int(grid_meters)
    owned: dict[str, list[str]] = {tile["slug"]: [] for tile in tiles}
    for (row, column), index in zip(keys.tolist(), owner.tolist()):
        owned[tiles[index]["slug"]].append(f"{grid}:{row}:{column}")
    return owned
