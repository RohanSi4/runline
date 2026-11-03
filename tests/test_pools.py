from __future__ import annotations

import base64
import gzip
import json
import sqlite3
import sys
from pathlib import Path

import networkx as nx
import pytest

from runline.models import RouteCandidate, RouteMetrics
from runline.pools import FORMAT_VERSION, decode_pool, encode_pool
from scripts import merge_pools
from scripts.precompute_pools import drop_near_duplicates

METRICS = {
    "distance_miles": 5.123,
    "elevation_gain_feet": 211.4,
    "traffic_signal_events": 3,
    "major_crossing_events": 1,
    "trail_fraction": 0.25,
    "repeated_fraction": 0.04,
    "drive_distance_miles": 0.0,
    "elevation_coverage": 1.0,
    "compactness": 0.612,
}


def _pool() -> dict:
    # OSM-sized ids, a loop that revisits a node, and one far from the rest.
    base = 6_100_000_000
    return {
        "origin_node": base + 3,
        "origin": [12.345678, -98.765432],
        "loops": [
            {"nodes": [base + 3, base + 4, base + 9, base + 4, base + 3], "metrics": METRICS},
            {"nodes": [base + 3, 11_002, base + 120_000, base + 3], "metrics": {**METRICS, "distance_miles": 7.5}},
        ],
    }


def test_round_trip_keeps_nodes_and_metrics() -> None:
    pool = _pool()
    decoded = decode_pool(encode_pool(pool))

    assert decoded["origin_node"] == pool["origin_node"]
    assert decoded["origin"] == pytest.approx(pool["origin"], abs=1e-6)
    assert [loop["nodes"] for loop in decoded["loops"]] == [loop["nodes"] for loop in pool["loops"]]
    for original, loop in zip(pool["loops"], decoded["loops"]):
        assert loop["metrics"] == pytest.approx(original["metrics"], abs=0.051)
        assert isinstance(loop["metrics"]["traffic_signal_events"], int)
    # Metrics must still build a RouteMetrics for ranking.
    RouteMetrics(**decoded["loops"][0]["metrics"])


def test_packed_is_much_smaller_than_gzipped_json() -> None:
    base = 6_100_000_000
    loops = [
        {"nodes": [base + (start + step) % 400 for step in range(300)], "metrics": METRICS}
        for start in range(0, 400, 20)
    ]
    pool = {"origin_node": base, "origin": [0.0, 0.0], "loops": loops}
    json_size = len(gzip.compress(json.dumps(pool, separators=(",", ":")).encode(), 6))

    assert len(encode_pool(pool)) * 3 < json_size


def test_rejects_other_versions() -> None:
    import lzma

    with pytest.raises(ValueError, match="pool format"):
        decode_pool(lzma.compress(bytes([FORMAT_VERSION + 1])))


def _candidate(nodes: tuple[int, ...], miles: float) -> RouteCandidate:
    return RouteCandidate(
        route_id=str(nodes),
        node_ids=nodes,
        coordinates=(),
        metrics=RouteMetrics(**{**METRICS, "distance_miles": miles}),
    )


def _ring(count: int) -> nx.DiGraph:
    graph = nx.DiGraph()
    for node in range(count):
        for u, v in ((node, (node + 1) % count), ((node + 1) % count, node)):
            graph.add_edge(u, v, length=100.0)
    for node in range(count):
        graph.add_edge(node, 100 + node, length=100.0)
        graph.add_edge(100 + node, node, length=100.0)
    return graph


def test_near_duplicates_across_targets_are_dropped() -> None:
    graph = _ring(10)
    ring = tuple(range(10)) + (0,)
    # Same ring with an out-and-back spur: shares 1,000 of 1,100 m.
    detour = tuple(range(6)) + (105,) + tuple(range(5, 10)) + (0,)

    kept = drop_near_duplicates(graph, [_candidate(ring, 5.0), _candidate(detour, 5.1)])

    assert [candidate.node_ids for candidate in kept] == [ring]


def test_overlapping_loops_far_apart_in_distance_are_kept() -> None:
    graph = _ring(10)
    ring = tuple(range(10)) + (0,)
    detour = tuple(range(6)) + (105,) + tuple(range(5, 10)) + (0,)

    kept = drop_near_duplicates(graph, [_candidate(ring, 5.0), _candidate(detour, 5.5)])

    assert len(kept) == 2


def test_distinct_loops_are_kept() -> None:
    graph = _ring(10)
    first = (0, 1, 2, 1, 0)
    second = (5, 6, 7, 6, 5)

    assert len(drop_near_duplicates(graph, [_candidate(first, 5.0), _candidate(second, 5.0)])) == 2


def _write_shard(path: Path, header: dict, pools: list[tuple[str, str, dict]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as stream:
        stream.write(json.dumps({"header": True, **header}) + "\n")
        for cell, surface, pool in pools:
            record = {
                "cell": cell,
                "surface": surface,
                "loops": len(pool["loops"]),
                "pool": base64.b64encode(encode_pool(pool)).decode("ascii"),
            }
            stream.write(json.dumps(record) + "\n")


HEADER = {
    "format": FORMAT_VERSION,
    "area": "test-area",
    "graph": "abc",
    "commit": "deadbee",
    "grid_meters": 200.0,
    "targets": [1, 2],
}


def _merge(tmp_path: Path, monkeypatch) -> int:
    monkeypatch.setattr(sys, "argv", ["merge_pools", "--area", "test-area", "--input", str(tmp_path)])
    return merge_pools.main()


def test_merge_stores_decodable_pools(tmp_path: Path, monkeypatch) -> None:
    _write_shard(tmp_path / "test-area" / "shard-00000.jsonl.gz", HEADER, [("200:1:2", "road", _pool())])
    _write_shard(tmp_path / "test-area" / "shard-00001.jsonl.gz", HEADER, [("200:1:3", "trail", _pool())])

    assert _merge(tmp_path, monkeypatch) == 0

    database = sqlite3.connect(tmp_path / "test-area.sqlite")
    rows = database.execute("SELECT cell, surface, loops, pool FROM pools ORDER BY cell").fetchall()
    assert [(cell, surface, loops) for cell, surface, loops, _ in rows] == [
        ("200:1:2", "road", 2),
        ("200:1:3", "trail", 2),
    ]
    assert decode_pool(rows[0][3])["origin_node"] == _pool()["origin_node"]
    meta = dict(database.execute("SELECT key, value FROM meta"))
    assert json.loads(meta["format"]) == FORMAT_VERSION


def test_merge_refuses_format_1_shards(tmp_path: Path, monkeypatch) -> None:
    _write_shard(tmp_path / "test-area" / "shard-00000.jsonl.gz", {**HEADER, "format": 1}, [])

    assert _merge(tmp_path, monkeypatch) == 1
    assert not (tmp_path / "test-area.sqlite").exists()
