from __future__ import annotations

import json
import math
import os
import tempfile
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import networkx as nx
import requests


OPEN_METEO_ELEVATION_URL = "https://api.open-meteo.com/v1/elevation"
OPEN_METEO_BATCH_SIZE = 100


def _coordinate_key(latitude: float, longitude: float) -> str:
    # The source DEM is 90 m resolution, so extra coordinate precision does not
    # improve the result. Three decimals keeps nearby graph nodes cacheable.
    return f"{latitude:.3f},{longitude:.3f}"


def _batched(values: list[str], size: int) -> Iterable[list[str]]:
    for index in range(0, len(values), size):
        yield values[index : index + size]


def _read_cache(path: Path) -> dict[str, float]:
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return {str(key): float(value) for key, value in raw.items()}
    except (ValueError, AttributeError):
        # A damaged cache is only lost work; refetching beats disabling elevation.
        return {}


def _write_cache(path: Path, cache: dict[str, float]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Concurrent requests share this file, so never leave it half-written.
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(json.dumps(cache, sort_keys=True) + "\n")
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def enrich_graph_open_meteo(
    graph: nx.Graph,
    cache_path: Path,
    *,
    node_ids: Iterable[Any] | None = None,
    session: Any = requests,
) -> int:
    """Attach terrain elevation in meters to graph nodes.

    Open-Meteo's elevation endpoint uses the Copernicus DEM GLO-90 dataset and
    accepts at most 100 coordinate pairs per request. Values are cached locally.
    Returns the number of coordinate cells fetched from the service.
    """

    cache = _read_cache(cache_path)
    nodes_by_key: dict[str, list[Any]] = {}
    coordinates_by_key: dict[str, tuple[float, float]] = {}
    selected_nodes = graph.nodes if node_ids is None else node_ids
    for node in selected_nodes:
        data = graph.nodes[node]
        latitude = float(data["y"])
        longitude = float(data["x"])
        key = _coordinate_key(latitude, longitude)
        nodes_by_key.setdefault(key, []).append(node)
        coordinates_by_key[key] = (latitude, longitude)

    missing = sorted(key for key in nodes_by_key if key not in cache)
    for keys in _batched(missing, OPEN_METEO_BATCH_SIZE):
        latitudes = ",".join(str(coordinates_by_key[key][0]) for key in keys)
        longitudes = ",".join(str(coordinates_by_key[key][1]) for key in keys)
        response = session.get(
            OPEN_METEO_ELEVATION_URL,
            params={"latitude": latitudes, "longitude": longitudes},
            timeout=30,
        )
        response.raise_for_status()
        elevations = response.json().get("elevation")
        if not isinstance(elevations, list) or len(elevations) != len(keys):
            raise ValueError("unexpected Open-Meteo elevation response")
        for key, elevation in zip(keys, elevations):
            cache[key] = float(elevation)
        _write_cache(cache_path, cache)

    for key, nodes in nodes_by_key.items():
        for node in nodes:
            graph.nodes[node]["elevation"] = cache[key]
    return len(missing)


COPERNICUS_TILE_URL = (
    "https://copernicus-dem-90m.s3.amazonaws.com/{name}/{name}.tif"
)


def _copernicus_tile_name(latitude: int, longitude: int) -> str:
    # Tiles are named by their south-west corner in whole degrees.
    north = f"{'N' if latitude >= 0 else 'S'}{abs(latitude):02d}_00"
    east = f"{'E' if longitude >= 0 else 'W'}{abs(longitude):03d}_00"
    return f"Copernicus_DSM_COG_30_{north}_{east}_DEM"


def _copernicus_tile(latitude: int, longitude: int, tile_directory: Path, session: Any) -> Path | None:
    """Download one GLO-90 tile once. Returns None for tiles with no land."""

    name = _copernicus_tile_name(latitude, longitude)
    path = tile_directory / f"{name}.tif"
    missing = tile_directory / f"{name}.missing"
    if path.exists():
        return path
    if missing.exists():
        return None
    tile_directory.mkdir(parents=True, exist_ok=True)
    response = session.get(COPERNICUS_TILE_URL.format(name=name), timeout=120)
    if response.status_code in (403, 404):
        # Open ocean has no tile; remember that instead of asking again.
        missing.touch()
        return None
    response.raise_for_status()
    # Per-process name: array tasks sharing a DEM directory may fetch the same
    # tile at once, and the last complete copy simply wins.
    temporary = path.with_suffix(f".{os.getpid()}.tmp")
    temporary.write_bytes(response.content)
    os.replace(temporary, path)
    return path


def enrich_graph_copernicus(
    graph: nx.Graph, tile_directory: Path, *, session: Any = requests
) -> int:
    """Attach elevation in meters from local Copernicus GLO-90 tiles.

    This is the same DEM Open-Meteo serves, read straight from the public AWS
    copy, so a bulk bake has no rate limit: Charlottesville needs one tile of
    about 10MB. Values are bilinearly interpolated between the four nearest
    cells. Nodes on tiles with no land keep no elevation, so coverage checks
    still see the gap. Returns the number of nodes given an elevation.
    """

    import numpy as np
    import rasterio

    nodes_by_tile: dict[tuple[int, int], list[Any]] = {}
    for node, data in graph.nodes(data=True):
        tile = (math.floor(float(data["y"])), math.floor(float(data["x"])))
        nodes_by_tile.setdefault(tile, []).append(node)

    baked = 0
    for (latitude, longitude), nodes in nodes_by_tile.items():
        path = _copernicus_tile(latitude, longitude, tile_directory, session)
        if path is None:
            continue
        with rasterio.open(path) as dataset:
            heights = dataset.read(1).astype(np.float64)
            nodata = dataset.nodata
            inverse = ~dataset.transform
        rows_max, columns_max = heights.shape[0] - 1, heights.shape[1] - 1
        for node in nodes:
            data = graph.nodes[node]
            column, row = inverse * (float(data["x"]), float(data["y"]))
            # The transform maps to cell corners; centres are half a cell in.
            column, row = column - 0.5, row - 0.5
            c0 = min(max(int(math.floor(column)), 0), columns_max)
            r0 = min(max(int(math.floor(row)), 0), rows_max)
            c1, r1 = min(c0 + 1, columns_max), min(r0 + 1, rows_max)
            fx = min(max(column - c0, 0.0), 1.0)
            fy = min(max(row - r0, 0.0), 1.0)
            corners = heights[[r0, r0, r1, r1], [c0, c1, c0, c1]]
            if nodata is not None and np.any(corners == nodata):
                continue
            top = corners[0] * (1 - fx) + corners[1] * fx
            bottom = corners[2] * (1 - fx) + corners[3] * fx
            data["elevation"] = float(top * (1 - fy) + bottom * fy)
            baked += 1
    return baked
