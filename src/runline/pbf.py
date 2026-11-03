"""Build walk graphs from a local OSM extract instead of Overpass.

Overpass cannot serve thousands of metro tiles, so tiles are cut from the
Geofabrik US extract (see scripts/build_tile_graphs.py) and read here with
pyosmium. The result is meant to be the graph ``osmnx.graph_from_point(...,
network_type="walk", simplify=True, retain_all=True)`` would have returned for
the same disc:

- ways pass osmnx's own walk filter, parsed from osmnx so the two cannot
  drift apart when osmnx changes it;
- elements are handed to osmnx's graph constructor in Overpass order (nodes,
  then ways, each by id), so node order and therefore tie-breaking in the
  planner match;
- the buffer, truncation, simplification and street counts follow
  ``osmnx.graph.graph_from_polygon`` step for step.

Only the walk graph is built. Pools are keyed by cell, so a tile needs no
drive graph, start list or place index.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

# osmnx's walk filter is an Overpass query string; only these two forms occur.
_RULE = re.compile(r'\["(?P<key>[^"]+)"(?:(?P<op>!~)"(?P<pattern>[^"]*)")?\]')


@lru_cache(maxsize=1)
def walk_filter_rules() -> tuple[tuple[str, "re.Pattern[str] | None"], ...]:
    """(key, pattern) pairs: pattern None means the key must exist; otherwise
    the way fails when the key is present and its value matches."""

    from osmnx import _overpass

    text = _overpass._get_network_filter("walk")
    rules = []
    position = 0
    for match in _RULE.finditer(text):
        if match.start() != position:
            raise ValueError(f"unsupported osmnx walk filter near {text[position:]!r}")
        position = match.end()
        pattern = match.group("pattern")
        rules.append((match.group("key"), re.compile(pattern) if match.group("op") else None))
    if position != len(text):
        raise ValueError(f"unsupported osmnx walk filter near {text[position:]!r}")
    return tuple(rules)


def is_walkable(tags: dict[str, str]) -> bool:
    for key, pattern in walk_filter_rules():
        value = tags.get(key)
        if pattern is None:
            if value is None:
                return False
        elif value is not None and pattern.search(value):
            return False
    return True


def overpass_elements(pbf: Path) -> tuple[dict, dict[int, int]]:
    """Walkable ways and the nodes they use, as an Overpass JSON response.

    Tile extracts are cut with osmium's ``simple`` strategy, which keeps ways
    whole but drops their nodes outside the box. A way is split into its runs
    of present nodes, so a way that leaves the box and comes back never joins
    its two ends with an edge that is not there. The extract box covers the
    buffer the graph is truncated to, so every node that survives truncation
    is present and the graph is the one complete ways would give. Second and
    later runs need their own element id; the returned map takes those
    (negative) ids back to the OSM way id.
    """

    import osmium

    ways = []
    used: set[int] = set()
    for way in osmium.FileProcessor(str(pbf), osmium.osm.WAY).with_filter(osmium.filter.KeyFilter("highway")):
        tags = {tag.k: tag.v for tag in way.tags}
        if not is_walkable(tags):
            continue
        nodes = [ref.ref for ref in way.nodes]
        used.update(nodes)
        ways.append({"type": "way", "id": way.id, "nodes": nodes, "tags": tags})

    nodes = []
    for node in osmium.FileProcessor(str(pbf), osmium.osm.NODE):
        if node.id in used and node.location.valid():
            element = {"type": "node", "id": node.id, "lat": node.location.lat, "lon": node.location.lon}
            if node.tags:
                element["tags"] = {tag.k: tag.v for tag in node.tags}
            nodes.append(element)

    nodes.sort(key=lambda element: element["id"])
    ways.sort(key=lambda element: element["id"])
    present = {element["id"] for element in nodes}
    pieces = []
    split_ids: dict[int, int] = {}
    for way in ways:
        runs, run = [], []
        for node in way["nodes"]:
            if node in present:
                run.append(node)
            else:
                if len(run) >= 2:
                    runs.append(run)
                run = []
        if len(run) >= 2:
            runs.append(run)
        for index, run in enumerate(runs):
            element_id = way["id"]
            if index:
                element_id = -(len(split_ids) + 1)
                split_ids[element_id] = way["id"]
            pieces.append({**way, "id": element_id, "nodes": run})
    return {"elements": nodes + pieces}, split_ids


def tile_polygons(latitude: float, longitude: float, radius_meters: float):
    """The bbox polygon graph_from_point keeps, and the 500 m buffer it downloads."""

    from osmnx import projection, utils_geo

    polygon = utils_geo.bbox_to_poly(utils_geo.bbox_from_point((latitude, longitude), radius_meters))
    projected, crs = projection.project_geometry(polygon)
    buffered, _ = projection.project_geometry(projected.buffer(500), crs=crs, to_latlong=True)
    return polygon, buffered


def extract_bounds(latitude: float, longitude: float, radius_meters: float) -> tuple[float, float, float, float]:
    """(west, south, east, north) an extract must cover to build this disc."""

    _, buffered = tile_polygons(latitude, longitude, radius_meters)
    return tuple(float(value) for value in buffered.bounds)


def walk_graph_from_pbf(pbf: Path, latitude: float, longitude: float, radius_meters: float):
    """The walk graph for a disc, or None when the extract has no walkable way."""

    import networkx as nx
    from osmnx import graph as osmnx_graph
    from osmnx import settings, simplification, stats, truncate

    response, split_ids = overpass_elements(pbf)
    if not any(element["type"] == "way" for element in response["elements"]):
        return None
    polygon, buffered = tile_polygons(latitude, longitude, radius_meters)

    bidirectional = "walk" in settings.bidirectional_network_types
    buffered_graph = osmnx_graph._create_graph([response], bidirectional)
    if split_ids:
        for _, _, data in buffered_graph.edges(data=True):
            data["osmid"] = split_ids.get(data["osmid"], data["osmid"])
    buffered_graph = truncate.truncate_graph_polygon(buffered_graph, buffered, truncate_by_edge=False)
    if buffered_graph.number_of_nodes() == 0:
        return None
    buffered_graph = simplification.simplify_graph(buffered_graph)
    graph = truncate.truncate_graph_polygon(buffered_graph, polygon, truncate_by_edge=False)
    street_counts = stats.count_streets_per_node(buffered_graph, nodes=graph.nodes)
    nx.set_node_attributes(graph, values=street_counts, name="street_count")
    return graph
