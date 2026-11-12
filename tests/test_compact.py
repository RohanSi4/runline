"""The compact graph format: exact round trips, identical planning, and hash checks."""

from __future__ import annotations

import dataclasses
import gzip
import hashlib
import json
import pickle
import shutil
import sqlite3
from pathlib import Path

import networkx as nx
import numpy as np
import pytest
from shapely.geometry import LineString

from runline import compact, coverage, serving
from runline.compact import (
    INVALID_FINGERPRINT,
    CompactGraphError,
    compact_path,
    compare_graphs,
    convert,
    graph_file,
    graph_fingerprint,
    read_compact,
    read_graph,
    write_compact,
)
from runline.coverage import CoverageError, areas, graphs_root, load_area_graph
from runline.geo import destination
from runline.models import RoutePreferences, SurfacePreference
from runline.osm import crop_to_radius
from runline.planner import plan_routes
from runline.point_to_point import plan_point_to_point
from runline.pools import FORMAT_VERSION
from scripts import compact_graphs

AREA_FILES = ("manifest.json", "charlottesville-va-starts.json", "charlottesville-va-places.json")


def _area():
    return next(item for item in areas() if item.slug == "charlottesville-va")


def _pickle(graph, path: Path) -> Path:
    path.write_bytes(gzip.compress(pickle.dumps(graph, protocol=5), 1, mtime=0))
    return path


def _sha16(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


@pytest.fixture(scope="module")
def small(tmp_path_factory) -> Path:
    """A 1.5 km crop of the shipped graph, pickled."""

    area = _area()
    graph = crop_to_radius(load_area_graph(area), area.center, 1_500)
    return _pickle(graph, tmp_path_factory.mktemp("small") / "small.pkl.gz")


@pytest.fixture(scope="module")
def compact_area(tmp_path_factory) -> dict:
    """The shipped area twice: as shipped, and with only its compact graph."""

    root = tmp_path_factory.mktemp("compact-area")
    pickled, compacted = root / "pickle", root / "compact"
    for directory in (pickled, compacted):
        directory.mkdir()
        for name in AREA_FILES:
            shutil.copy(graphs_root() / name, directory / name)
    source = graphs_root() / _area().filename
    shutil.copy(source, pickled / source.name)
    header = convert(source, compacted / compact_path(source).name, verify=True)
    assert header["problems"] == []
    return {"pickle": pickled, "compact": compacted, "fingerprint": _sha16(source)}


def _use_graphs(monkeypatch, directory: Path) -> None:
    monkeypatch.setenv("RUNLINE_GRAPHS_DIR", str(directory))
    monkeypatch.setenv("RUNLINE_ALLOW_OSM_DOWNLOAD", "0")
    coverage._read_manifest.cache_clear()
    coverage._load_graph_file.cache_clear()
    serving._checked_pool_file.cache_clear()
    from runline import osm

    osm._PREPARED_CACHE.clear()


@pytest.fixture(autouse=True)
def _fresh_caches():
    yield
    coverage._read_manifest.cache_clear()
    coverage._load_graph_file.cache_clear()
    serving._checked_pool_file.cache_clear()
    from runline import osm

    osm._PREPARED_CACHE.clear()


# --- the format ----------------------------------------------------------------


def test_the_shipped_graph_round_trips_exactly(compact_area) -> None:
    original = compact.read_pickle(graphs_root() / _area().filename)
    restored = read_compact(compact_area["compact"] / "charlottesville-va.npz")

    assert compare_graphs(original, restored) == []
    # Adjacency order, which search tie-breaks follow, is the pickle's.
    node = next(iter(original))
    assert list(original.succ[node]) == list(restored.succ[node])
    assert all(list(original.pred[n]) == list(restored.pred[n]) for n in original)
    assert restored.graph["elevation_baked"] is True


def test_the_compact_file_is_smaller_than_the_pickle(compact_area) -> None:
    source = graphs_root() / _area().filename
    assert (compact_area["compact"] / "charlottesville-va.npz").stat().st_size < 0.5 * source.stat().st_size


def test_unused_attributes_are_dropped(compact_area) -> None:
    restored = read_compact(compact_area["compact"] / "charlottesville-va.npz")
    edge_keys = set().union(*(data.keys() for _, _, data in restored.edges(data=True)))
    node_keys = set().union(*(data.keys() for _, data in restored.nodes(data=True)))

    assert edge_keys <= {*compact.EDGE_TAGS, *compact.EDGE_FLOATS, "geometry"}
    assert "osmid" not in edge_keys and "oneway" not in edge_keys
    assert node_keys <= {"x", "y", *compact.NODE_TAGS, *compact.NODE_FLOATS}
    assert "street_count" not in node_keys


def _awkward_graph() -> nx.MultiDiGraph:
    """Lists, missing tags, parallel edges, a self-loop, an isolated node and
    predecessor order that differs from the order edges are listed in."""

    graph = nx.MultiDiGraph(crs="epsg:4326", elevation_baked=False, created_with="test")
    graph.add_node(30, y=1 / 3, x=-2 / 3, highway="traffic_signals", elevation=12.5)
    graph.add_node(10, y=38.1, x=-78.4, crossing="marked;uncontrolled")
    graph.add_node(20, y=38.2, x=-78.5, **{"crossing:signals": "yes"}, street_count=3)
    graph.add_node(40, y=38.3, x=-78.6)
    graph.add_edge(20, 10, key=0, length=5.0, highway=["footway", "path"], name=["A", "B"])
    graph.add_edge(10, 20, key=0, length=5.0, highway="footway", osmid=[1, 2],
                   geometry=LineString([(-78.4, 38.1), (-78.45, 38.15), (-78.5, 38.2)]))
    graph.add_edge(10, 20, key=1, length=7.25, highway="primary", ref="US 1", bridge="yes")
    graph.add_edge(20, 30, key=0, length=1.0, highway="service",
                   geometry=LineString([(-78.5, 38.2), (-2 / 3, 1 / 3)]))
    graph.add_edge(30, 20, key=3, length=1.0, highway="service", tunnel="culvert",
                   geometry=LineString([(-2 / 3, 1 / 3), (-78.5, 38.2)]))
    graph.add_edge(30, 30, key=0, length=0.5, highway="path", surface="dirt")
    graph.add_edge(10, 30, key=0, highway="path")
    return graph


def test_awkward_graphs_round_trip_with_their_order(tmp_path) -> None:
    graph = _awkward_graph()
    path = tmp_path / "awkward.npz"
    write_compact(graph, path, source_fingerprint="0123456789abcdef")
    restored = read_compact(path)

    assert compare_graphs(graph, restored) == []
    assert list(restored.pred[20]) == list(graph.pred[20]) == [10, 30]
    assert list(restored[10][20]) == [0, 1] and list(restored[30][20]) == [3]
    assert restored.nodes[10]["crossing"] == "marked;uncontrolled"
    assert restored.edges[20, 10, 0]["highway"] == ["footway", "path"]
    assert "length" not in restored.edges[10, 30, 0] and "elevation" not in restored.nodes[10]
    # Off-grid coordinates are kept as float64, not rounded to OSM's grid.
    assert restored.nodes[30]["y"] == 1 / 3
    assert list(restored.edges[20, 30, 0]["geometry"].coords) == [(-78.5, 38.2), (-2 / 3, 1 / 3)]
    assert list(restored.edges[30, 20, 3]["geometry"].coords) == [(-2 / 3, 1 / 3), (-78.5, 38.2)]
    # The same key dict on both sides, as networkx builds it.
    assert restored._pred[20][10] is restored._succ[10][20]
    assert restored.graph == {"crs": "epsg:4326", "elevation_baked": False, "created_with": "test"}


def test_a_changed_attribute_is_reported(tmp_path) -> None:
    graph = _awkward_graph()
    path = tmp_path / "awkward.npz"
    write_compact(graph, path, source_fingerprint="0" * 16)
    restored = read_compact(path)
    restored.edges[10, 20, 1]["length"] = 7.250000001
    restored.nodes[10]["crossing"] = "marked"

    problems = compare_graphs(graph, restored)
    assert any("length" in problem for problem in problems)
    assert any("crossing" in problem for problem in problems)


def test_values_json_cannot_carry_are_refused(tmp_path) -> None:
    graph = _awkward_graph()
    graph.edges[10, 20, 0]["name"] = {"not": "a tag"}
    with pytest.raises(CompactGraphError):
        write_compact(graph, tmp_path / "bad.npz", source_fingerprint="0" * 16)
    assert not (tmp_path / "bad.npz").exists()


def test_only_multidigraphs_are_converted(tmp_path) -> None:
    with pytest.raises(CompactGraphError):
        write_compact(nx.DiGraph([(1, 2)]), tmp_path / "plain.npz", source_fingerprint="0" * 16)


# --- hashes and choosing the file ---------------------------------------------


def _tamper(path: Path) -> None:
    """Change one length but keep the recorded hash, as a bad copy or edit would."""

    with np.load(path, allow_pickle=False) as archive:
        arrays = {name: archive[name] for name in archive.files}
    arrays["edge_length"] = arrays["edge_length"].copy()
    arrays["edge_length"][0] += 1.0
    with path.open("wb") as handle:
        np.savez_compressed(handle, **arrays)


def test_a_compact_file_carries_its_pickles_fingerprint(small, tmp_path) -> None:
    target = tmp_path / "small.npz"
    convert(small, target, verify=True)

    assert graph_fingerprint(target) == _sha16(small) == graph_fingerprint(small)
    header = compact.read_compact_header(target)
    assert header["source_name"] == "small.pkl.gz" and len(header["content_sha256"]) == 64


def test_a_tampered_compact_file_is_refused(small, tmp_path) -> None:
    target = tmp_path / "small.npz"
    convert(small, target)
    _tamper(target)

    assert graph_fingerprint(target) == INVALID_FINGERPRINT
    with pytest.raises(CompactGraphError, match="recorded hash"):
        read_compact(target)


def test_a_truncated_or_flipped_compact_file_is_refused(small, tmp_path) -> None:
    target = tmp_path / "small.npz"
    convert(small, target)
    payload = bytearray(target.read_bytes())
    flipped = tmp_path / "flipped.npz"
    payload[len(payload) // 2] ^= 0xFF
    flipped.write_bytes(bytes(payload))
    truncated = tmp_path / "truncated.npz"
    truncated.write_bytes(bytes(payload[: len(payload) // 3]))

    for path in (flipped, truncated):
        assert graph_fingerprint(path) == INVALID_FINGERPRINT
        with pytest.raises(CompactGraphError):
            read_graph(path)


def test_loading_prefers_a_current_compact_file(small, tmp_path) -> None:
    source = shutil.copy(small, tmp_path / "tile.pkl.gz")
    assert graph_file(source) == Path(source)

    convert(source, verify=True)
    assert graph_file(source) == tmp_path / "tile.npz"

    Path(source).unlink()
    assert graph_file(source) == tmp_path / "tile.npz"


def test_a_compact_file_from_another_pickle_does_not_shadow_it(small, tmp_path) -> None:
    source = Path(shutil.copy(small, tmp_path / "tile.pkl.gz"))
    convert(source)
    graph = compact.read_pickle(source)
    graph.graph["rebuilt"] = True
    _pickle(graph, source)

    assert graph_file(source) == source


def test_a_damaged_compact_file_beside_its_pickle_falls_back_to_the_pickle(small, tmp_path) -> None:
    source = Path(shutil.copy(small, tmp_path / "tile.pkl.gz"))
    convert(source)
    _tamper(tmp_path / "tile.npz")

    assert graph_file(source) == source


# --- serving and planning on the compact file ----------------------------------


def _pool_file(directory: Path, fingerprint: str) -> None:
    database = sqlite3.connect(directory / "charlottesville-va.sqlite")
    database.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    meta = {"format": FORMAT_VERSION, "graph": fingerprint, "grid_meters": 200.0}
    database.executemany("INSERT INTO meta VALUES (?, ?)", [(k, json.dumps(v)) for k, v in meta.items()])
    database.commit()
    database.close()


def test_a_pool_built_on_the_pickle_validates_against_its_compact_file(compact_area, tmp_path, monkeypatch) -> None:
    _pool_file(tmp_path, compact_area["fingerprint"])
    monkeypatch.setenv("RUNLINE_POOLS_DIR", str(tmp_path))

    for directory in ("pickle", "compact"):
        _use_graphs(monkeypatch, compact_area[directory])
        assert serving.pool_file(_area()) is not None, directory


def test_a_pool_from_another_graph_is_still_refused(compact_area, tmp_path, monkeypatch) -> None:
    _pool_file(tmp_path, "0" * 16)
    monkeypatch.setenv("RUNLINE_POOLS_DIR", str(tmp_path))
    _use_graphs(monkeypatch, compact_area["compact"])

    assert serving.pool_file(_area()) is None


def test_a_tampered_compact_area_graph_is_refused_and_reported(compact_area, tmp_path, monkeypatch) -> None:
    graphs = tmp_path / "graphs"
    shutil.copytree(compact_area["compact"], graphs)
    _tamper(graphs / "charlottesville-va.npz")
    pools = tmp_path / "pools"
    pools.mkdir()
    _pool_file(pools, compact_area["fingerprint"])
    monkeypatch.setenv("RUNLINE_POOLS_DIR", str(pools))
    _use_graphs(monkeypatch, graphs)

    assert serving.pool_file(_area()) is None
    with pytest.raises(CoverageError, match="damaged"):
        load_area_graph(_area())


def _plans(directory: Path, monkeypatch, tmp_path) -> list:
    _use_graphs(monkeypatch, directory)
    area = _area()
    results = []
    for miles, surface, bearing in ((3, "mixed", 0), (4, "road", 120), (5, "trail", 240)):
        origin = destination(area.center, bearing, 1_200)
        preferences = RoutePreferences(miles, surface=SurfacePreference(surface))
        result = plan_routes(origin, preferences, cache_directory=tmp_path, elevation_source="none")
        results.append([dataclasses.asdict(candidate) for candidate in result.candidates] + list(result.warnings))
    finish = destination(area.center, 45, 2_500)
    direct = plan_point_to_point(area.center, finish, RoutePreferences(3), cache_directory=tmp_path)
    results.append([dataclasses.asdict(candidate) for candidate in direct.candidates])
    return results


def test_live_planning_is_identical_on_the_compact_graph(compact_area, monkeypatch, tmp_path) -> None:
    from_pickle = _plans(compact_area["pickle"], monkeypatch, tmp_path)
    from_compact = _plans(compact_area["compact"], monkeypatch, tmp_path)

    assert all(plan for plan in from_pickle)
    assert from_compact == from_pickle


# --- the converter ---------------------------------------------------------------


def test_the_converter_writes_verifies_skips_and_checks(small, tmp_path, capsys) -> None:
    directory = tmp_path / "tiles"
    directory.mkdir()
    for name in ("a", "b"):
        shutil.copy(small, directory / f"{name}.pkl.gz")

    assert compact_graphs.main([str(directory), "--verify"]) == 0
    assert sorted(path.name for path in directory.iterdir()) == ["a.npz", "a.pkl.gz", "b.npz", "b.pkl.gz"]
    assert "2 graphs, 0 failed" in capsys.readouterr().out

    assert compact_graphs.main([str(directory)]) == 0
    assert capsys.readouterr().out.count(": kept ok") == 2

    assert compact_graphs.main([str(directory), "--check"]) == 0
    _tamper(directory / "b.npz")
    assert compact_graphs.main([str(directory), "--check"]) == 1
    assert "recorded hash" in capsys.readouterr().out

    # Not current, so it is rewritten.
    assert compact_graphs.main([str(directory), "--verify"]) == 0
    assert compact_graphs.main([str(directory), "--check"]) == 0


def test_the_converter_can_write_elsewhere(small, tmp_path) -> None:
    output = tmp_path / "out"
    assert compact_graphs.main([str(small), "--output", str(output), "--verify"]) == 0
    assert graph_fingerprint(output / "small.npz") == _sha16(small)
    assert not compact_path(small).exists()
