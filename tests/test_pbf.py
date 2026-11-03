"""The PBF builder must give the graph osmnx would build from Overpass.

A synthetic extract stands in for Geofabrik: a grid of ways near an arbitrary
point at sea, with ways each walk-filter rule should drop. osmnx's own
graph_from_point is then run on the same data (its Overpass download patched
to return what the walk filter keeps), and the two graphs must be identical.
"""

from __future__ import annotations

import json
import gzip
import pickle
from pathlib import Path

import networkx as nx
import pytest

osmium = pytest.importorskip("osmium")
pytest.importorskip("osmnx")

from runline.pbf import (  # noqa: E402
    extract_bounds,
    is_walkable,
    overpass_elements,
    walk_graph_from_pbf,
)
from scripts import build_tile_graphs  # noqa: E402

LATITUDE, LONGITUDE = 10.0, 20.0
STEP = 0.001  # about 110 m
RADIUS = 600.0

# id: tags. Ways 1-12 form a walkable grid; the rest must be filtered out.
DROPPED = {
    101: {"highway": "cycleway"},
    102: {"highway": "motorway"},
    103: {"highway": "residential", "foot": "no"},
    104: {"highway": "service", "service": "private"},
    105: {"highway": "footway", "access": "private"},
    106: {"highway": "primary", "sidewalk": "separate"},
    107: {"highway": "pedestrian", "area": "yes"},
    108: {"building": "yes"},
}


def _node_id(row: int, column: int) -> int:
    return 1000 + row * 100 + column


def _write_extract(path: Path) -> None:
    from osmium.osm.mutable import Node, Way

    writer = osmium.SimpleWriter(str(path))
    try:
        for row in range(-12, 13):
            for column in range(-12, 13):
                tags = {"highway": "traffic_signals"} if (row, column) == (0, 0) else {}
                writer.add_node(
                    Node(
                        id=_node_id(row + 12, column + 12),
                        location=(LONGITUDE + column * STEP, LATITUDE + row * STEP),
                        tags=tags,
                    )
                )
        # An amenity node no way uses: Overpass would not return it.
        writer.add_node(Node(id=99_999, location=(LONGITUDE, LATITUDE + 0.0005), tags={"amenity": "bench"}))
        way_id = 1
        for row in range(-12, 13, 4):
            refs = [_node_id(row + 12, column + 12) for column in range(-12, 13)]
            writer.add_way(Way(id=way_id, nodes=refs, tags={"highway": "residential", "name": f"Row {row}"}))
            way_id += 1
        for column in range(-12, 13, 4):
            refs = [_node_id(row + 12, column + 12) for row in range(-12, 13)]
            writer.add_way(Way(id=way_id, nodes=refs, tags={"highway": "footway"}))
            way_id += 1
        for offset, (way, tags) in enumerate(DROPPED.items()):
            row = -10 + 2 * offset
            refs = [_node_id(row + 12, column + 12) for column in range(2, 9)]
            writer.add_way(Way(id=way, nodes=refs, tags=tags))
    finally:
        writer.close()


@pytest.fixture(scope="module")
def extract(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("pbf") / "tile.osm.pbf"
    _write_extract(path)
    return path


@pytest.mark.parametrize("tags", DROPPED.values())
def test_walk_filter_drops(tags) -> None:
    assert not is_walkable(tags)


@pytest.mark.parametrize(
    "tags",
    [
        {"highway": "residential"},
        {"highway": "footway", "foot": "yes"},
        {"highway": "path", "access": "permissive"},
        {"highway": "primary", "sidewalk": "both"},
    ],
)
def test_walk_filter_keeps(tags) -> None:
    assert is_walkable(tags)


def test_elements_hold_only_walkable_ways_and_their_nodes(extract: Path) -> None:
    elements = overpass_elements(extract)["elements"]
    ways = {element["id"] for element in elements if element["type"] == "way"}
    nodes = [element["id"] for element in elements if element["type"] == "node"]

    assert ways == set(range(1, 15))
    assert 99_999 not in nodes
    assert nodes == sorted(nodes)
    # Overpass order: every node before any way.
    kinds = [element["type"] for element in elements]
    assert kinds == sorted(kinds)


def test_graph_matches_osmnx_graph_from_point(extract: Path, monkeypatch) -> None:
    import osmnx as ox
    from osmnx import _overpass

    response = overpass_elements(extract)
    monkeypatch.setattr(_overpass, "_download_overpass_network", lambda *args, **kwargs: iter([response]))
    expected = ox.graph.graph_from_point(
        (LATITUDE, LONGITUDE), dist=RADIUS, network_type="walk", simplify=True, retain_all=True
    )

    graph = walk_graph_from_pbf(extract, LATITUDE, LONGITUDE, RADIUS)

    assert list(graph.nodes(data=True)) == list(expected.nodes(data=True))
    assert sorted(graph.edges(keys=True)) == sorted(expected.edges(keys=True))
    for u, v, key, data in expected.edges(keys=True, data=True):
        mine = graph.edges[u, v, key]
        assert set(mine) == set(data)
        assert mine["length"] == pytest.approx(data["length"])
        assert mine["osmid"] == data["osmid"]
    # The graph is cut to the requested bbox, not the whole extract.
    assert 0 < graph.number_of_nodes() < 25 * 25
    assert graph.nodes[_node_id(12, 12)]["highway"] == "traffic_signals"


def test_empty_extract_gives_none(tmp_path: Path) -> None:
    from osmium.osm.mutable import Node

    path = tmp_path / "empty.osm.pbf"
    writer = osmium.SimpleWriter(str(path))
    writer.add_node(Node(id=1, location=(LONGITUDE, LATITUDE)))
    writer.close()

    assert walk_graph_from_pbf(path, LATITUDE, LONGITUDE, RADIUS) is None


def test_extract_bounds_cover_the_buffered_disc() -> None:
    west, south, east, north = extract_bounds(LATITUDE, LONGITUDE, RADIUS)
    # 600 m plus the 500 m buffer, in degrees at 10 N.
    assert north - LATITUDE == pytest.approx(1100 / 110_574, rel=0.02)
    assert LONGITUDE - west == pytest.approx(1100 / (111_320 * 0.9848), rel=0.02)


def test_build_writes_graph_and_stats(extract: Path, tmp_path: Path, monkeypatch) -> None:
    metros = tmp_path / "metros.json"
    tile = {"slug": "test-000", "metro": "test", "latitude": LATITUDE, "longitude": LONGITUDE, "radius_meters": RADIUS}
    metros.write_text(json.dumps({"metros": [{"slug": "test", "tiles": [tile]}]}))
    extracts = tmp_path / "extracts"
    extracts.mkdir()
    (extracts / "test-000.osm.pbf").write_bytes(extract.read_bytes())
    # No DEM download in tests.
    monkeypatch.setattr(build_tile_graphs, "bake", lambda graph, dem_dir: 0.0)
    output = tmp_path / "graphs"

    arguments = ["--extracts", str(extracts), "--output", str(output), "--metros", str(metros)]
    assert build_tile_graphs.main(["build", *arguments]) == 0
    assert build_tile_graphs.main(["build", *arguments]) == 0  # second run skips

    stats = json.loads((output / "test-000.json").read_text())
    with gzip.open(output / "test-000.pkl.gz", "rb") as handle:
        graph = pickle.load(handle)
    assert stats["nodes"] == graph.number_of_nodes() > 0
    assert isinstance(graph, nx.MultiDiGraph)
    assert build_tile_graphs.main(["summary", "--output", str(output)]) == 0


def test_configs_write_one_extract_per_tile(tmp_path: Path) -> None:
    tiles = [
        {"slug": f"test-{index:03d}", "latitude": LATITUDE, "longitude": LONGITUDE + index * 0.1, "radius_meters": RADIUS}
        for index in range(5)
    ]
    metros = tmp_path / "metros.json"
    metros.write_text(json.dumps({"metros": [{"slug": "test", "tiles": tiles}]}))
    extracts = tmp_path / "extracts"

    assert build_tile_graphs.main(
        ["configs", "--pbf", "us.osm.pbf", "--extracts", str(extracts), "--metros", str(metros), "--batch-size", "2"]
    ) == 0

    configs = sorted(extracts.glob("extract-*.json"))
    assert len(configs) == 3
    outputs = [entry["output"] for path in configs for entry in json.loads(path.read_text())["extracts"]]
    assert outputs == [f"test-{index:03d}.osm.pbf" for index in range(5)]
    script = (extracts / "extract.sh").read_text()
    assert script.count("osmium extract") == 3 and "tags-filter" in script
