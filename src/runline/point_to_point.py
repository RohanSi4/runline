"""Direct running paths on a locally available graph.

Unlike loop planning, this keeps one-way components and does not crop around
the start or apply a loop-distance/shape objective. A shipped area holding
both endpoints is used first. Otherwise a metro tile graph whose disc holds
both endpoints is used (runline.metros.direct_tiles); paths are never stitched
across tiles. Regional loop backends are not consulted here: the API forwards
a direct request only to a backend whose /api/capabilities reports direct
support, because older APIs can silently ignore destination fields.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import networkx as nx

from .coverage import Area, CoverageError, areas, load_area_graph
from .geo import distance_meters
from .metros import direct_metro_labels, direct_tiles
from .models import Coordinate, RouteCandidate, RoutePreferences, StartCandidate
from .osm import _nearest_node, _path, _route_coordinates, collapse_graph, measure_route
from .planner import PlanResult


MAX_SNAP_METERS = 300.0
SNAP_NOTICE_METERS = 25.0
# Tile graphs take seconds and hundreds of MB to load, and the tile cache
# holds two by default, so a request tries at most this many tiles.
MAX_TILE_ATTEMPTS = 2


class _OffNetwork(CoverageError):
    """An endpoint is beyond the snapping limit on this graph."""


class _NoPath(ValueError):
    """This graph has no path from the start to the finish."""


def direct_area_labels() -> tuple[str, ...]:
    """Where start -> finish works on this server: shipped areas, then metros with local tile graphs."""

    return tuple(dict.fromkeys((*(area.label for area in areas()), *direct_metro_labels())))


def _uncovered(metros: tuple[str, ...] = ()) -> CoverageError:
    message = "Choose a start and finish inside the same supported area."
    if metros:
        message += (
            f" In {', '.join(metros)}, direct routes need both endpoints on one map tile;"
            " choose a finish closer to the start."
        )
    labels = ", ".join(direct_area_labels())
    if labels:
        message += f" Available direct-route coverage: {labels}."
    return CoverageError(message)


def plan_point_to_point(
    origin: Coordinate,
    destination: Coordinate,
    preferences: RoutePreferences,
    *,
    cache_directory: Path,
) -> PlanResult:
    """Return one weighted direct path, with actual mapped start/end points.

    ``cache_directory`` keeps the planner interface consistent; this function
    uses only the already cached local graph and performs no downloads. Surface
    changes search costs. Target distance, hills and drive-radius preferences
    are not direct-path objectives and should be hidden in this mode's UI.
    """

    covering = [area for area in areas() if area.covers(origin, 0) and area.covers(destination, 0)]
    if covering:
        area = min(covering, key=lambda item: item.radius_meters)
        return _plan_on(area, origin, destination, preferences)

    tiles = direct_tiles(origin, destination)
    if not tiles.areas:
        raise _uncovered(tiles.metros)
    present = [area for area in tiles.areas if Path(area.filename).exists()]
    if not present:
        raise CoverageError(f"Map data for {tiles.areas[0].label} is missing from this deployment.")
    first_error: Exception | None = None
    for area in present[:MAX_TILE_ATTEMPTS]:
        try:
            return _plan_on(area, origin, destination, preferences)
        except (_OffNetwork, _NoPath) as error:
            # Near a tile's edge its neighbour can hold more of the map.
            first_error = first_error or error
    raise first_error


def _plan_on(area: Area, origin: Coordinate, destination: Coordinate, preferences: RoutePreferences) -> PlanResult:
    # collapse_graph creates a new graph. No SCC pruning: a valid one-way path
    # need not have a way back, and the cached source graph stays untouched.
    graph = collapse_graph(load_area_graph(area), preferences)
    if not graph.number_of_nodes():
        raise _OffNetwork(f"No walkable map is available for {area.label}.")

    start_node = _nearest_node(graph, origin)
    finish_node = _nearest_node(graph, destination)
    snapped_start = Coordinate(float(graph.nodes[start_node]["y"]), float(graph.nodes[start_node]["x"]))
    snapped_finish = Coordinate(float(graph.nodes[finish_node]["y"]), float(graph.nodes[finish_node]["x"]))
    snap_distances = (distance_meters(origin, snapped_start), distance_meters(destination, snapped_finish))
    for name, meters in zip(("start", "finish"), snap_distances):
        if meters > MAX_SNAP_METERS:
            raise _OffNetwork(
                f"The {name} is too far from a mapped walkable path. Choose a point closer to a street or trail."
            )
    if start_node == finish_node:
        raise ValueError("The start and finish map to the same point. Choose a finish farther away.")

    try:
        nodes = _path(graph, start_node, finish_node)
    except (nx.NetworkXNoPath, nx.NodeNotFound):
        raise _NoPath("No walkable path connects this start and finish. Try different locations.") from None

    # Existing route measurement handles open paths; compactness would close
    # the outline mathematically, so it is neutralized and never used to rank.
    metrics = replace(measure_route(graph, nodes), compactness=0.0)
    candidate = RouteCandidate(
        route_id="route-001",
        node_ids=tuple(nodes),
        coordinates=_route_coordinates(graph, nodes),
        metrics=metrics,
        start=StartCandidate("Start here", "origin", snapped_start, 0),
        score=sum(float(graph.edges[u, v]["_cost"]) for u, v in zip(nodes, nodes[1:])),
    )
    candidate.score_components = {"path_cost": candidate.score}
    warnings = tuple(
        f"The {name} was moved {meters:.0f} m to the nearest mapped walkable path."
        for name, meters in zip(("start", "finish"), snap_distances)
        if meters > SNAP_NOTICE_METERS
    )
    return PlanResult(
        candidates=[candidate], discovered_starts=0, fetched_elevation_cells=0,
        warnings=warnings, source="live",
    )
