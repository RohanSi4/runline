"""Label major-road crossings from geometry, independently of the route proxy.

The route's 15 m corridor is cut along every major-road centreline from the
full shipped area, leaving out bridges and tunnels the route never touches.
The pieces are the local "sides" of those roads, and a crossing is the route
moving from one piece to another, sampled every 4 m. Same-side turns stay in
one piece and dead-end roads do not cut the corridor. A move made on a route
bridge/tunnel, or under a major-road bridge, does not count unless the route
touches the major road. Changes within 40 m of travel are one event if they
end on a different side, so a divided road counts once.

The corridor is narrow on purpose: at 60 m, sides joined around the end of a
major-road segment tens of metres away (Rugby Road at Beta Bridge), hiding
real crossings.

This labels crossings of mapped centrelines. It does not know whether a
crossing is marked, signalled, or busy.

    .venv/bin/python scripts/audit_crossings.py   # rewrites the test fixture
"""

from __future__ import annotations

import json
import math
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if __name__ == "__main__":
    sys.path.insert(0, str(ROOT / "src"))

import numpy as np
import shapely
from shapely.geometry import LineString
from shapely.ops import unary_union

CORRIDOR_METERS = 15.0
STEP_METERS = 4.0
MERGE_METERS = 40.0
CONTEXT_METERS = 50.0
_GRADE = {"bridge", "tunnel"}
_index = None


def _separated(data) -> bool:
    from runline.osm import _values

    return any(_values(data.get(key)) - {"no"} for key in _GRADE)


def _major_index():
    """Projected major-road lines of the whole shipped area, with their ends
    and whether they are bridges or tunnels."""

    global _index
    if _index is None:
        from runline.coverage import areas, load_area_graph
        from runline.osm import _is_major

        area = areas()[0]
        graph = load_area_graph(area)
        scale = (math.cos(math.radians(area.center.latitude)) * 111_320, 111_320)
        origin = (area.center.longitude, area.center.latitude)
        lines, ends, seen = [], [], set()
        for u, v, _, data in graph.edges(keys=True, data=True):
            if not _is_major(data) or (min(u, v), max(u, v)) in seen:
                continue
            seen.add((min(u, v), max(u, v)))
            geometry = data.get("geometry")
            points = (list(geometry.coords) if geometry is not None else
                      [(graph.nodes[n]["x"], graph.nodes[n]["y"]) for n in (u, v)])
            lines.append(LineString([((x - origin[0]) * scale[0], (y - origin[1]) * scale[1])
                                     for x, y in points]))
            ends.append((u, v, _separated(data)))
        _index = (shapely.STRtree(lines), lines, ends, origin, scale)
    return _index


def _samples(graph, node_ids):
    """Points every ~4 m, tagged with the route edge they lie on."""

    from runline.osm import _edge_geometry_coordinates

    _, _, _, origin, scale = _major_index()
    xs, ys, edge_index, lines = [], [], [], []
    for i, (u, v) in enumerate(zip(node_ids, node_ids[1:])):
        points = [((c.longitude - origin[0]) * scale[0], (c.latitude - origin[1]) * scale[1])
                  for c in _edge_geometry_coordinates(graph, u, v)]
        line = LineString(points)
        lines.append(line)
        count = max(1, round(line.length / STEP_METERS))
        for k in range(count):
            point = line.interpolate((k + 0.5) * line.length / count)
            xs.append(point.x)
            ys.append(point.y)
            edge_index.append(i)
    return np.array(xs), np.array(ys), edge_index, lines


def side_trace(graph, node_ids):
    """(side piece id or None, route edge index, travel metres) per sample."""

    tree, majors, ends, _, _ = _major_index()
    xs, ys, edge_index, lines = _samples(graph, node_ids)
    corridor = unary_union(lines).buffer(CORRIDOR_METERS)
    # A bridge or tunnel the route shares a node with is at the route's grade.
    on_route = set(node_ids)
    nearby = [majors[i] for i in tree.query(corridor)
              if not ends[i][2] or ends[i][0] in on_route or ends[i][1] in on_route]
    pieces = corridor.difference(unary_union(nearby).buffer(1.0)) if nearby else corridor
    pieces = list(getattr(pieces, "geoms", [pieces]))
    side = np.full(len(xs), -1)
    for number, piece in enumerate(pieces):
        side[shapely.contains_xy(piece, xs, ys)] = number
    travel = np.concatenate([[0], np.cumsum(np.hypot(np.diff(xs), np.diff(ys)))])
    return [(None if s < 0 else int(s), e, float(t)) for s, e, t in zip(side, edge_index, travel)]


def _changes(graph, node_ids, trace):
    """Side changes as (travel, from, to, last edge before, first edge after)."""

    from runline.osm import _is_major

    separated = [_separated(graph.edges[u, v]) for u, v in zip(node_ids, node_ids[1:])]
    touches = [any(_is_major(data) for data in graph.succ[node].values()) for node in node_ids]
    changes, previous = [], None
    for side, edge, travel in trace:
        if side is None:
            continue
        if previous is not None and side != previous[0]:
            # Passing over or under on a route bridge/tunnel is not a crossing,
            # unless the route touches the major road on the way.
            if not any(separated[previous[1]:edge + 1]) or any(touches[previous[1] + 1:edge + 1]):
                changes.append((travel, previous[0], side, previous[1], edge))
        previous = (side, edge)
    return changes


def _label_slice(graph, node_ids) -> list[tuple[int, int]]:
    changes = _changes(graph, node_ids, side_trace(graph, node_ids))
    events, cluster = [], []
    for change in changes + [None]:
        if cluster and (change is None or change[0] - cluster[-1][0] > MERGE_METERS):
            if cluster[0][1] != cluster[-1][2]:
                first = cluster[0][3] + 1
                events.append((first, max(first, cluster[-1][4])))
            cluster = []
        if change is not None:
            cluster.append(change)
    return events


def label_events(graph, node_ids) -> list[tuple[int, int]]:
    """Geometric crossings as (first, last) route positions of the nodes involved.

    Each slice around a major road is labelled on its own corridor. A whole-route
    corridor joins the two sides of a road wherever the route passes under a
    bridge or around a road end elsewhere, which hid 8 real crossings.
    """

    return [(lo + first, lo + last) for lo, hi in _spans(graph, node_ids)
            for first, last in _label_slice(graph, node_ids[lo:hi + 1])]


def geometric_crossings(graph, node_ids) -> int:
    return len(label_events(graph, node_ids))


def match(proxy: list[int], labels: list[tuple[int, int]]) -> tuple[int, int, int]:
    """(true positive, false positive, false negative) events, one node of slack."""

    remaining = list(labels)
    tp = 0
    for position in proxy:
        hit = next((event for event in remaining if event[0] - 1 <= position <= event[1] + 1), None)
        if hit:
            remaining.remove(hit)
            tp += 1
    return tp, len(proxy) - tp, len(remaining)


def _spans(graph, node_ids):
    """Merged (first, last) route positions around every node on a major road,
    extended at least CONTEXT_METERS of travel each way."""

    from runline.osm import _is_major

    lengths = [float(graph.edges[u, v].get("length", 0)) for u, v in zip(node_ids, node_ids[1:])]
    spans = []
    for j, node in enumerate(node_ids):
        if not any(_is_major(data) for data in graph.succ[node].values()):
            continue
        lo, hi, back, ahead = j, j, 0.0, 0.0
        while lo > 0 and back < CONTEXT_METERS:
            lo -= 1
            back += lengths[lo]
        while hi < len(node_ids) - 1 and ahead < CONTEXT_METERS:
            ahead += lengths[hi]
            hi += 1
        spans.append((lo, hi))
    merged = []
    for lo, hi in sorted(spans):
        if merged and lo <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], hi)
        else:
            merged.append([lo, hi])
    return merged


def windows(graph, node_ids, labels):
    """The labelled route slices, each with its crossings."""

    return [{"nodes": list(node_ids[lo:hi + 1]),
             "events": [[first - lo, last - lo] for first, last in labels
                        if lo <= first and last <= hi]}
            for lo, hi in _spans(graph, node_ids)]


def main():
    os.environ["RUNLINE_ALLOW_OSM_DOWNLOAD"] = "0"
    from runline.geo import destination
    from runline.models import Coordinate, RoutePreferences
    from runline.osm import _major_crossings, generate_loops, graph_radius_meters, prepare_core

    center = Coordinate(38.029306, -78.4766781)
    origins = [Coordinate(38.035556, -78.503333), center] + [
        destination(center, bearing, 2_500) for bearing in range(0, 360, 60)
    ]
    totals = [0, 0, 0]
    slices = []
    for origin in origins:
        for miles in (3, 5, 8, 12):
            preference = RoutePreferences(miles)
            graph = prepare_core(origin, graph_radius_meters(preference), ROOT / "cache/audit", preference)
            for route in generate_loops(graph, origin, preference):
                nodes = list(route.node_ids)
                labels = label_events(graph, nodes)
                for i, value in enumerate(match(_major_crossings(graph, nodes), labels)):
                    totals[i] += value
                slices += [dict(item, miles=miles) for item in windows(graph, nodes, labels)]
    tp, fp, fn = totals
    print(f"routes, event level: tp={tp} fp={fp} fn={fn} "
          f"precision={tp / (tp + fp):.3f} recall={tp / (tp + fn):.3f}")
    unique = list({tuple(item["nodes"]): item for item in slices}.values())
    (ROOT / "tests/fixtures/crossing_audit.json").write_text(json.dumps(unique) + "\n")
    print(f"fixture: {len(unique)} route slices, "
          f"{sum(len(item['events']) for item in unique)} geometric crossings")


if __name__ == "__main__":
    main()
