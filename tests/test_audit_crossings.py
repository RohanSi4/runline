"""Hand-labelled geometry cases for the offline crossing audit."""

import networkx as nx
import shapely
from shapely.geometry import LineString

from scripts import audit_crossings


def test_geometry_labels_a_side_change_but_not_a_same_side_turn(monkeypatch) -> None:
    # The route approaches from the west. Main Street runs north-south.
    graph = nx.DiGraph()
    for node, (x, y) in {
        1: (-30, 0), 2: (0, 0), 3: (30, 0), 4: (-30, 10),
        5: (0, 40), 6: (0, -40),
    }.items():
        graph.add_node(node, x=x / 111_320, y=y / 111_320)
    for u, v, kind in (
        (1, 2, "footway"), (2, 3, "footway"), (2, 4, "footway"),
        (2, 5, "primary"), (2, 6, "primary"),
    ):
        graph.add_edge(u, v, highway=kind, length=30)
    lines = [LineString([(0, 0), (0, 40)]), LineString([(0, 0), (0, -40)])]
    monkeypatch.setattr(
        audit_crossings, "_index",
        (shapely.STRtree(lines), lines, [(2, 5, False), (2, 6, False)],
         (0, 0), (111_320, 111_320)),
    )

    assert audit_crossings.label_events(graph, [1, 2, 3]) == [(1, 1)]
    assert audit_crossings.label_events(graph, [1, 2, 4]) == []
