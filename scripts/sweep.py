"""Offline 720-pair-per-distance opportunity sweep on the shipped graph."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
os.environ["RUNLINE_ALLOW_OSM_DOWNLOAD"] = "0"

import networkx as nx

from runline.geo import destination
from runline.models import Coordinate, RouteCandidate, RoutePreferences
from runline.osm import (
    _distinct_by_length,
    _join_paths,
    _nearest_node,
    _path,
    generate_loops,
    graph_radius_meters,
    measure_route,
    prepare_core,
)
from runline.scoring import rank_candidates


def describe(route, target):
    metrics = route.metrics
    return {
        "waypoints": route.route_id,
        "error_miles": abs(metrics.distance_miles - target),
        "signals": metrics.traffic_signal_events,
        "crossings": metrics.major_crossing_events,
        "repeated_fraction": metrics.repeated_fraction,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--origin", choices=("rotunda", "center"), default="rotunda")
    parser.add_argument("--distances", nargs="+", type=int, default=(3, 5, 8, 12))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    origin = (Coordinate(38.035556, -78.503333) if args.origin == "rotunda"
              else Coordinate(38.029306, -78.4766781))
    results = []
    for miles in args.distances:
        preferences = RoutePreferences(miles)
        graph = prepare_core(origin, graph_radius_meters(preferences), ROOT / "cache/sweep", preferences)
        start = _nearest_node(graph, origin)
        outbound = nx.single_source_dijkstra_path(graph, start, weight="_cost")
        inbound = nx.single_source_dijkstra_path(graph.reverse(copy=False), start, weight="_cost")
        sampled = 0
        candidates = []
        started = time.perf_counter()
        for scale in (.5, .65, .8, .95, 1.1, 1.25):
            radius = preferences.target_distance_meters / 3 * scale
            for heading in range(0, 360, 15):
                for sweep in (60, 90, 120, 150, 180):
                    sampled += 1
                    first = _nearest_node(graph, destination(origin, heading - sweep / 2, radius))
                    second = _nearest_node(graph, destination(origin, heading + sweep / 2, radius))
                    if len({start, first, second}) < 3:
                        continue
                    try:
                        middle = _path(graph, first, second)
                        nodes = _join_paths([outbound[first], middle, list(reversed(inbound[second]))])
                    except (KeyError, nx.NetworkXNoPath, nx.NodeNotFound):
                        continue
                    metrics = measure_route(graph, nodes)
                    if .55 * miles <= metrics.distance_miles <= 1.5 * miles:
                        candidates.append(RouteCandidate(f"{scale}:{heading}:{sweep}", tuple(nodes), (), metrics))
        feasible = [route for route in candidates if abs(route.metrics.distance_miles - miles) <= preferences.distance_tolerance_miles]
        sweep_top = _distinct_by_length(graph, rank_candidates(feasible, preferences), 3)
        generated = generate_loops(graph, origin, preferences)
        result = {
            "origin": args.origin, "miles": miles, "sampled_pairs": sampled,
            "candidate_count": len(candidates), "feasible_count": len(feasible),
            "sweep_top": [describe(route, miles) for route in sweep_top],
            "generated_top": [describe(route, miles) for route in generated],
            "seconds": time.perf_counter() - started,
        }
        results.append(result)
        print(args.origin, miles, sampled, len(feasible), round(result["seconds"], 2), flush=True)
    payload = json.dumps(results, indent=2) + "\n"
    if args.output:
        args.output.write_text(payload)
    else:
        print(payload)


if __name__ == "__main__":
    main()
