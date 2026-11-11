"""Signalled crosswalks must survive both graph builders and be counted.

The layout is the common sidewalk mapping: a road tagged sidewalk=separate
(which osmnx's walk filter drops), a sidewalk either side of it, and short
crossing footways between them. Each crossing node then joins exactly two
edges, which osmnx's simplification would fold away along with its tags.
"""

from __future__ import annotations

import gzip
import pickle
from pathlib import Path

import pytest

pytest.importorskip("osmnx")

from runline.models import RoutePreferences  # noqa: E402
from runline.osm import collapse_graph, measure_route  # noqa: E402

LATITUDE, LONGITUDE = 10.0, 20.0
STEP = 0.0005  # about 55 m
RADIUS = 400.0

SIGNALLED = 1001  # crossing=traffic_signals
FLAGGED = 1002  # crossing=marked, crossing:signals=yes
UNSIGNALLED = 1003  # crossing=uncontrolled
NODE_TAGS = {
    SIGNALLED: {"highway": "crossing", "crossing": "traffic_signals"},
    FLAGGED: {"highway": "crossing", "crossing": "marked", "crossing:signals": "yes"},
    UNSIGNALLED: {"highway": "crossing", "crossing": "uncontrolled"},
}
CROSSING_ROWS = {0: SIGNALLED, 3: FLAGGED, -3: UNSIGNALLED}


def _sidewalk(column: int, row: int) -> int:
    return 2000 + (column + 1) * 100 + row + 10


def _road(row: int) -> int:
    return CROSSING_ROWS.get(row, 3000 + row + 10)


def _elements() -> list[dict]:
    """Nodes then ways, by id, as Overpass returns them."""

    rows = range(-6, 7)
    nodes = {}
    for row in rows:
        latitude = LATITUDE + row * STEP
        for column in (-1, 1):
            nodes[_sidewalk(column, row)] = (latitude, LONGITUDE + column * STEP / 4)
        nodes[_road(row)] = (latitude, LONGITUDE)
    elements = []
    for node, (latitude, longitude) in sorted(nodes.items()):
        element = {"type": "node", "id": node, "lat": latitude, "lon": longitude}
        if node in NODE_TAGS:
            element["tags"] = NODE_TAGS[node]
        elements.append(element)
    ways = [
        (1, [_road(row) for row in rows], {"highway": "primary", "sidewalk": "separate", "name": "Main"}),
        (2, [_sidewalk(-1, row) for row in rows], {"highway": "footway", "footway": "sidewalk"}),
        (3, [_sidewalk(1, row) for row in rows], {"highway": "footway", "footway": "sidewalk"}),
    ]
    for offset, row in enumerate(sorted(CROSSING_ROWS)):
        ways.append(
            (10 + offset, [_sidewalk(-1, row), _road(row), _sidewalk(1, row)], {"highway": "footway", "footway": "crossing"})
        )
    for way, refs, tags in ways:
        elements.append({"type": "way", "id": way, "nodes": refs, "tags": tags})
    return elements


def _overpass_response() -> list[dict]:
    """What Overpass returns for the walk filter: walkable ways and their nodes."""

    from runline.pbf import is_walkable

    ways = [element for element in _elements() if element["type"] == "way" and is_walkable(element["tags"])]
    used = {node for way in ways for node in way["nodes"]}
    return [element for element in _elements() if element["type"] == "node" and element["id"] in used] + ways


def _assert_signals_kept_and_counted(graph) -> None:
    assert SIGNALLED in graph and FLAGGED in graph
    assert graph.nodes[SIGNALLED]["crossing"] == "traffic_signals"
    assert graph.nodes[FLAGGED]["crossing:signals"] == "yes"
    # An uncontrolled crossing is simplified away as before.
    assert UNSIGNALLED not in graph
    assert not any(key.startswith("_runline") for _, data in graph.nodes(data=True) for key in data)

    collapsed = collapse_graph(graph, RoutePreferences(target_distance_miles=1))
    west, east = _sidewalk(-1, 0), _sidewalk(1, 0)
    assert measure_route(collapsed, [west, SIGNALLED, east]).traffic_signal_events == 1
    assert collapsed.edges[west, SIGNALLED]["_cost"] >= 450


def test_overpass_builder_keeps_signalled_crossings(tmp_path: Path, monkeypatch) -> None:
    from osmnx import _overpass

    from scripts import build_graphs

    response = {"elements": _overpass_response()}
    monkeypatch.setattr(_overpass, "_download_overpass_network", lambda *args, **kwargs: iter([response]))
    monkeypatch.setattr(build_graphs, "GRAPHS_DIR", tmp_path)
    monkeypatch.setattr(build_graphs, "bake_elevation", lambda slug, graph: None)
    monkeypatch.setattr(build_graphs, "build_start_candidates", lambda *args: [])
    area = {"slug": "test", "label": "Test", "latitude": LATITUDE, "longitude": LONGITUDE, "radius_meters": RADIUS}

    build_graphs.build_area(area, force=True)

    with gzip.open(tmp_path / "test.pkl.gz", "rb") as handle:
        graph = pickle.load(handle)
    _assert_signals_kept_and_counted(graph)
    # The gzip header carries no build time, so a rebuild hashes the same.
    assert (tmp_path / "test.pkl.gz").read_bytes()[4:8] == b"\0\0\0\0"


def test_pbf_builder_keeps_signalled_crossings(tmp_path: Path) -> None:
    osmium = pytest.importorskip("osmium")
    from osmium.osm.mutable import Node, Way

    from runline.pbf import walk_graph_from_pbf

    path = tmp_path / "tile.osm.pbf"
    writer = osmium.SimpleWriter(str(path))
    try:
        for element in _elements():
            if element["type"] == "node":
                writer.add_node(
                    Node(id=element["id"], location=(element["lon"], element["lat"]), tags=element.get("tags", {}))
                )
            else:
                writer.add_way(Way(id=element["id"], nodes=element["nodes"], tags=element["tags"]))
    finally:
        writer.close()

    graph = walk_graph_from_pbf(path, LATITUDE, LONGITUDE, RADIUS)

    _assert_signals_kept_and_counted(graph)


def test_builders_leave_osmnx_settings_alone(tmp_path: Path) -> None:
    from osmnx import settings

    from runline.pbf import tile_polygons, walk_graph_from_responses

    before = list(settings.useful_tags_node)
    polygon, buffered = tile_polygons(LATITUDE, LONGITUDE, RADIUS)
    walk_graph_from_responses([{"elements": _overpass_response()}], polygon, buffered)

    assert settings.useful_tags_node == before
