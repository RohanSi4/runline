"""Build reproducible crossing labels from the shipped graph's local geometry.

The label is an interior intersection of a 15 m route chord with a major-road
line. Route edges tagged major are excluded. This is a geometric label, not a
claim that every OSM road intersection has a pedestrian crossing facility.
"""

from __future__ import annotations

import json
import math
import os
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
os.environ["RUNLINE_ALLOW_OSM_DOWNLOAD"] = "0"

from shapely.geometry import LineString

from runline.geo import distance_meters
from runline.models import Coordinate, RoutePreferences
from runline.osm import (
    _edge_geometry_coordinates,
    _is_major,
    generate_loops,
    graph_radius_meters,
    prepare_core,
)


def local_points(graph, u, v, node):
    points = _edge_geometry_coordinates(graph, u, v)
    if v == node:
        points.reverse()
    center = graph.nodes[node]
    scale = math.cos(math.radians(center["y"])) * 111_320
    return [((point.longitude - center["x"]) * scale,
             (point.latitude - center["y"]) * 111_320) for point in points]


def direction(points, metres=15):
    travelled = 0.0
    for first, second in zip(points, points[1:]):
        length = math.dist(first, second)
        if length and travelled + length >= metres:
            fraction = (metres - travelled) / length
            return (first[0] + fraction * (second[0] - first[0]),
                    first[1] + fraction * (second[1] - first[1]))
        travelled += length
    return points[-1]


def geometry_label(graph, a, b, c, nearby_major_nodes):
    if _is_major(graph.edges[a, b]) or _is_major(graph.edges[b, c]):
        return False
    before = direction(local_points(graph, a, b, b))
    after = direction(local_points(graph, b, c, b))
    if math.dist(before, after) < 1:
        return False
    route = LineString([before, (0, 0), after])
    for road_node in nearby_major_nodes:
        directions = []
        visited = set()
        for u, v, data in list(graph.in_edges(road_node, data=True)) + list(graph.out_edges(road_node, data=True)):
            other = u if v == road_node else v
            if other in visited or not _is_major(data):
                continue
            visited.add(other)
            points = local_points(graph, u, v, road_node)
            outward = direction(points)
            center = graph.nodes[b]
            road = graph.nodes[road_node]
            scale = math.cos(math.radians(center["y"])) * 111_320
            offset = ((road["x"] - center["x"]) * scale,
                      (road["y"] - center["y"]) * 111_320)
            directions.append((offset[0] + outward[0], offset[1] + outward[1], offset))
        for i, first in enumerate(directions):
            for second in directions[i + 1:]:
                center = first[2]
                v1 = (first[0] - center[0], first[1] - center[1])
                v2 = (second[0] - center[0], second[1] - center[1])
                norms = math.hypot(*v1) * math.hypot(*v2)
                if norms == 0 or (v1[0] * v2[0] + v1[1] * v2[1]) / norms > -.7:
                    continue
                major = LineString([(first[0], first[1]), center,
                                    (second[0], second[1])])
                if route.crosses(major):
                    return True
    return False


def main():
    origin = Coordinate(38.035556, -78.503333)
    positives, negatives = [], []
    seen = set()
    for miles in (3, 5, 8, 12):
        preference = RoutePreferences(miles, result_count=10)
        graph = prepare_core(origin, graph_radius_meters(preference), ROOT / "cache/audit", preference)
        major_nodes = set()
        for u, v, data in graph.edges(data=True):
            if _is_major(data):
                major_nodes.update((u, v))
        bins = defaultdict(list)
        for node in major_nodes:
            data = graph.nodes[node]
            bins[(round(data["y"] * 1000), round(data["x"] * 1000))].append(node)
        for route in generate_loops(graph, origin, preference):
            for a, b, c in zip(route.node_ids, route.node_ids[1:], route.node_ids[2:]):
                if (a, b, c) in seen:
                    continue
                seen.add((a, b, c))
                data = graph.nodes[b]
                cell = (round(data["y"] * 1000), round(data["x"] * 1000))
                nearby = [node for dy in (-1, 0, 1) for dx in (-1, 0, 1)
                          for node in bins.get((cell[0] + dy, cell[1] + dx), ())
                          if distance_meters(Coordinate(data["y"], data["x"]),
                                             Coordinate(graph.nodes[node]["y"], graph.nodes[node]["x"])) <= 30]
                if not nearby:
                    continue
                proxy = b in major_nodes and not _is_major(graph.edges[a, b]) and not _is_major(graph.edges[b, c])
                label = geometry_label(graph, a, b, c, nearby)
                item = {"miles": miles, "nodes": [a, b, c], "proxy": proxy, "geometry_crossing": label}
                (positives if proxy else negatives).append(item)
    sample = positives[:50] + negatives[:50]
    output = ROOT / "tests/fixtures/crossing_audit.json"
    output.parent.mkdir(exist_ok=True)
    output.write_text(json.dumps(sample, indent=2) + "\n")
    tp = sum(item["proxy"] and item["geometry_crossing"] for item in sample)
    fp = sum(item["proxy"] and not item["geometry_crossing"] for item in sample)
    fn = sum(not item["proxy"] and item["geometry_crossing"] for item in sample)
    print(f"sample={len(sample)} positive_pool={len(positives)} negative_pool={len(negatives)} "
          f"tp={tp} fp={fp} fn={fn} precision={tp/(tp+fp):.3f} recall={tp/(tp+fn):.3f}")


if __name__ == "__main__":
    main()
