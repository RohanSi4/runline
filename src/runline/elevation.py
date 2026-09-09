from __future__ import annotations

import json
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
    raw = json.loads(path.read_text(encoding="utf-8"))
    return {str(key): float(value) for key, value in raw.items()}


def _write_cache(path: Path, cache: dict[str, float]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cache, sort_keys=True) + "\n", encoding="utf-8")


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
