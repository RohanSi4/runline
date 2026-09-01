from __future__ import annotations

import json

import networkx as nx

from runroute.elevation import enrich_graph_open_meteo


class _Response:
    def __init__(self, elevations: list[float]) -> None:
        self.elevations = elevations

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, list[float]]:
        return {"elevation": self.elevations}


class _Session:
    def __init__(self) -> None:
        self.calls = 0

    def get(self, *_args, params, **_kwargs) -> _Response:
        self.calls += 1
        return _Response([100 + index for index, _ in enumerate(params["latitude"].split(","))])


def test_enriches_nodes_and_reuses_local_cache(tmp_path) -> None:
    graph = nx.Graph()
    graph.add_node(1, x=-78.50001, y=38.00001)
    graph.add_node(2, x=-78.50002, y=38.00002)
    graph.add_node(3, x=-78.51, y=38.01)
    session = _Session()
    cache = tmp_path / "elevation.json"

    fetched = enrich_graph_open_meteo(graph, cache, session=session)

    assert fetched == 2
    assert session.calls == 1
    assert graph.nodes[1]["elevation"] == graph.nodes[2]["elevation"] == 100
    assert graph.nodes[3]["elevation"] == 101
    assert len(json.loads(cache.read_text(encoding="utf-8"))) == 2

    second_session = _Session()
    assert enrich_graph_open_meteo(graph, cache, session=second_session) == 0
    assert second_session.calls == 0
