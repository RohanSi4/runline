"""Render the README's figures; the map figures come from real RunLine output.

    python scripts/readme_figures.py                 # writes docs/readme/*.svg
    python scripts/readme_figures.py architecture    # just the deployment diagram

The architecture diagram is drawn from the deployment description below and
needs no graph. Every map figure is drawn from the shipped Charlottesville
graph and the real planner, starting at the area's map centre from graphs/manifest.json, so no
coordinates live in this file. SVG is written directly (no plotting library)
in the app's palette, with positions in local metres rounded to the pixel.
"""

from __future__ import annotations

import argparse
import math
import os
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))
os.environ.setdefault("RUNLINE_ALLOW_OSM_DOWNLOAD", "0")

import networkx as nx  # noqa: E402

from runline.coverage import areas, load_area_graph  # noqa: E402
from runline.geo import distance_meters  # noqa: E402
from runline.grid import cell_id  # noqa: E402
from runline.models import Coordinate, RouteCandidate, RouteMetrics, RoutePreferences, SurfacePreference  # noqa: E402
from runline.osm import (  # noqa: E402
    _edge_geometry_coordinates,
    _is_major,
    _nearest_node,
    collapse_graph,
    generate_loops,
    signal_events,
)
from runline.scoring import rank_candidates  # noqa: E402
from scripts.precompute_pools import cell_pool, prepare_area  # noqa: E402

OUT = ROOT / "docs" / "readme"
CANVAS, PANEL, INK, MUTED, LINE = "#f4f1e9", "#fbfaf6", "#20221f", "#62655d", "#deddd6"
GREEN, GREEN_2, LIME, ORANGE, RED = "#193c2c", "#2f6348", "#c8e663", "#e2552f", "#a33225"
ROUTES = ("#e2552f", "#235f9f", "#7a4d96")
FONT = 'font-family="DM Sans, Inter, -apple-system, Segoe UI, sans-serif"'
TARGET_MILES = 5
METERS_PER_MILE = 1609.344


class Frame:
    """Local metres around a centre, scaled into a width x height viewBox."""

    def __init__(self, centre: Coordinate, half_width_m: float, width: int, height: int) -> None:
        self.centre, self.width, self.height = centre, width, height
        self.scale = (width / 2) / half_width_m
        self.x_metres = 111_320.0 * math.cos(math.radians(centre.latitude))

    def xy(self, latitude: float, longitude: float) -> tuple[float, float]:
        dx = (longitude - self.centre.longitude) * self.x_metres
        dy = (latitude - self.centre.latitude) * 110_540.0
        return self.width / 2 + dx * self.scale, self.height / 2 - dy * self.scale

    def inside(self, x: float, y: float, margin: float = 40) -> bool:
        return -margin <= x <= self.width + margin and -margin <= y <= self.height + margin

    def path(self, points) -> str:
        parts, last = [], None
        for index, point in enumerate(points):
            here = tuple(round(value) for value in self.xy(point.latitude, point.longitude))
            if here == last:
                continue
            parts.append(("M" if index == 0 else "L") + f"{here[0]} {here[1]}")
            last = here
        return "".join(parts)


def fitted(points, width: int, height: int, padding: float = 1.12) -> Frame:
    """A frame centred on the points' bounding box with all of them inside."""

    latitudes = [p.latitude for p in points]
    longitudes = [p.longitude for p in points]
    centre = Coordinate((min(latitudes) + max(latitudes)) / 2, (min(longitudes) + max(longitudes)) / 2)
    x_metres = 111_320.0 * math.cos(math.radians(centre.latitude))
    half_w = (max(longitudes) - min(longitudes)) * x_metres / 2
    half_h = (max(latitudes) - min(latitudes)) * 110_540.0 / 2
    return Frame(centre, max(half_w, half_h * width / height) * padding, width, height)


def svg(width: int, height: int, title: str, body: list[str]) -> str:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" role="img" aria-label="{title}">'
        f'<clipPath id="card"><rect width="{width}" height="{height}" rx="22"/></clipPath>'
        f'<g clip-path="url(#card)"><rect width="{width}" height="{height}" fill="{CANVAS}"/>'
        + "".join(body)
        + "</g></svg>\n"
    )


def text(x: float, y: float, content: str, *, size: float = 14, colour: str = INK, weight: int = 500,
         anchor: str = "start", halo: bool = True) -> str:
    """A label with a paper-coloured halo so it stays legible over streets."""

    outline = f' paint-order="stroke" stroke="{CANVAS}" stroke-width="4" stroke-linejoin="round"' if halo else ""
    return (f'<text x="{x:.0f}" y="{y:.0f}" {FONT} font-size="{size}" font-weight="{weight}" '
            f'fill="{colour}" text-anchor="{anchor}"{outline}>{content}</text>')


def panel(x: float, y: float, width: float, height: float) -> str:
    return (f'<rect x="{x}" y="{y}" width="{width}" height="{height}" rx="10" fill="{PANEL}" '
            f'fill-opacity="0.95" stroke="{LINE}"/>')


def streets(graph, frame: Frame, *, curves: bool, minor: str = LINE, major: str = "#d3d1c8") -> list[str]:
    """One path per class: minor streets thin, major roads wider."""

    seen, minor_d, major_d = set(), [], []
    for u, v, data in graph.edges(data=True):
        key = (min(u, v), max(u, v))
        if key in seen:
            continue
        seen.add(key)
        ends = [frame.xy(float(graph.nodes[n]["y"]), float(graph.nodes[n]["x"])) for n in (u, v)]
        if not any(frame.inside(*end, margin=60) for end in ends):
            continue
        points = _edge_geometry_coordinates(graph, u, v) if curves else node_coordinates(graph, (u, v))
        (major_d if _is_major(data) else minor_d).append(frame.path(points))
    return [
        f'<path d="{"".join(minor_d)}" fill="none" stroke="{minor}" stroke-width="1.2" stroke-linecap="round"/>',
        f'<path d="{"".join(major_d)}" fill="none" stroke="{major}" stroke-width="3.2" stroke-linecap="round"/>',
    ]


def route_path(frame: Frame, points, colour: str, width: float, casing: bool = True, opacity: float = 1,
               dash: str | None = None) -> list[str]:
    d = frame.path(points)
    dashed = f' stroke-dasharray="{dash}"' if dash else ""
    out = []
    if casing:
        out.append(f'<path d="{d}" fill="none" stroke="{PANEL}" stroke-width="{width + 4.5}" '
                   'stroke-linejoin="round" stroke-linecap="round"/>')
    out.append(f'<path d="{d}" fill="none" stroke="{colour}" stroke-width="{width}" '
               f'stroke-linejoin="round" stroke-linecap="round" opacity="{opacity}"{dashed}/>')
    return out


def pin(frame: Frame, at: Coordinate, label: str | None = None) -> str:
    x, y = frame.xy(at.latitude, at.longitude)
    mark = (f'<g transform="translate({x:.0f} {y:.0f})"><circle r="11" fill="{GREEN}"/>'
            f'<circle r="7.5" fill="none" stroke="{LIME}" stroke-width="1.8"/><circle r="3.2" fill="{LIME}"/></g>')
    return mark + (text(x + 18, y + 5, label, size=14, weight=600) if label else "")


def scale_bar(frame: Frame, x: float, y: float) -> str:
    """A bar of a round length near 160 px, labelled in km and miles."""

    metres = 160 / frame.scale
    nice = min((100, 200, 250, 500, 1000, 2000, 5000), key=lambda value: abs(value - metres))
    width = nice * frame.scale
    label = f"{nice / 1000:g} km" if nice >= 1000 else f"{nice} m"
    return (f'<rect x="{x}" y="{y}" width="{width:.0f}" height="5" rx="2" fill="{INK}"/>'
            + text(x, y - 7, f"{label} · {nice / METERS_PER_MILE:.2g} mi", size=12, colour=MUTED, weight=500))


def node_coordinates(graph, nodes):
    return [Coordinate(float(graph.nodes[n]["y"]), float(graph.nodes[n]["x"])) for n in nodes]


def mix(a: str, b: str, t: float) -> str:
    pa = [int(a[i : i + 2], 16) for i in (1, 3, 5)]
    pb = [int(b[i : i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x + (y - x) * t):02x}" for x, y in zip(pa, pb))


def distance_colour(miles: float) -> str:
    t = max(0.0, min(1.0, (miles - 1) / 11))
    return mix("#a9cf3b", GREEN_2, t / 0.5) if t < 0.5 else mix(GREEN_2, GREEN, (t - 0.5) / 0.5)


# --- figures -----------------------------------------------------------------

def loops_figure(core, drawn, start: Coordinate) -> tuple[str, list]:
    routes = generate_loops(core, start, RoutePreferences(TARGET_MILES, surface=SurfacePreference.MIXED))
    frame = fitted([point for route in routes for point in route.coordinates], 1140, 640)
    body = streets(drawn, frame, curves=True)
    for index in reversed(range(len(routes))):
        body += route_path(frame, routes[index].coordinates, ROUTES[index], 5 if index == 0 else 3.5,
                           opacity=1 if index == 0 else 0.85)
    body.append(pin(frame, start, "Start / finish"))
    body.append(panel(24, 24, 214, 30 + 26 * len(routes)))
    for index, route in enumerate(routes):
        y = 52 + 26 * index
        body.append(f'<rect x="40" y="{y - 7}" width="26" height="6" rx="3" fill="{ROUTES[index]}"/>')
        body.append(text(76, y, f"Option {index + 1} · {route.metrics.distance_miles:.2f} mi", size=14, halo=False))
    body.append(scale_bar(frame, 1140 - 40 - 200, 600))
    title = f"The {len(routes)} {TARGET_MILES} mile loops RunLine returns from the Charlottesville map centre, on the shipped walk graph"
    return svg(1140, 640, title, body), routes


def graph_figure(graph, start: Coordinate) -> str:
    """The same blocks as mapped (every shape point) and as searched (intersections only)."""

    width, height, half = 540, 400, 130
    panels, counts = [], []
    for offset, searched in ((0, False), (600, True)):
        frame = Frame(start, half, width, height)
        lines, dots, seen, nodes, shape_points = [], [], set(), set(), 0
        for u, v in graph.edges():
            key = (min(u, v), max(u, v))
            if key in seen:
                continue
            seen.add(key)
            ends = [frame.xy(float(graph.nodes[n]["y"]), float(graph.nodes[n]["x"])) for n in (u, v)]
            if not any(frame.inside(*end, margin=0) for end in ends):
                continue
            nodes.update((u, v))
            points = _edge_geometry_coordinates(graph, u, v)
            lines.append(frame.path(points))
            for point in points[1:-1]:
                x, y = frame.xy(point.latitude, point.longitude)
                if frame.inside(x, y, margin=0):
                    shape_points += 1
                    if not searched:
                        dots.append(f'<circle cx="{x:.0f}" cy="{y:.0f}" r="3.2" fill="{PANEL}" stroke="{MUTED}" stroke-width="1.3"/>')
        visible = 0
        for n in nodes:
            x, y = frame.xy(float(graph.nodes[n]["y"]), float(graph.nodes[n]["x"]))
            if frame.inside(x, y, margin=0):
                visible += 1
                dots.append(f'<circle cx="{x:.0f}" cy="{y:.0f}" r="4.6" fill="{GREEN}"/>')
        counts.append((visible, shape_points))
        label = (f"As mapped: {visible + shape_points} points" if not searched
                 else f"As searched: {visible} intersections")
        panels.append(
            f'<g transform="translate({offset} 0)"><clipPath id="c{offset}"><rect width="{width}" height="{height}" rx="16"/></clipPath>'
            f'<rect width="{width}" height="{height}" rx="16" fill="{PANEL}" stroke="{LINE}"/>'
            f'<g clip-path="url(#c{offset})"><path d="{"".join(lines)}" fill="none" stroke="#9a9c94" '
            f'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/>{"".join(dots)}'
            f'{panel(14, 14, 250, 34)}{text(28, 37, label, size=14, weight=600, halo=False)}</g></g>'
        )
    title = (f"The same blocks twice: as mapped with every shape point ({sum(counts[0])} points), "
             f"and as the planner searches them, intersections only ({counts[1][0]}), with the same curved streets")
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1140 400" role="img" aria-label="{title}">'
            + "".join(panels) + "</svg>\n")


def path_cost(graph, nodes, weight: str) -> float:
    return sum(float(graph.edges[u, v][weight]) for u, v in zip(nodes, nodes[1:]))


def major_share(graph, nodes) -> float:
    total = path_cost(graph, nodes, "length") or 1.0
    return sum(float(graph.edges[u, v]["length"]) for u, v in zip(nodes, nodes[1:])
               if _is_major(graph.edges[u, v])) / total


def busy_and_quiet(graph, start: Coordinate):
    """Two points near the centre where the shortest way is busy and the cheapest is not."""

    rng = random.Random(4)
    # Search a few thousand pairs; only a handful meet every condition.
    near = [n for n, data in graph.nodes(data=True)
            if distance_meters(start, Coordinate(float(data["y"]), float(data["x"]))) < 1500]
    best = None
    for _ in range(3000):
        a, b = rng.sample(near, 2)
        gap = distance_meters(*node_coordinates(graph, (a, b)))
        if not 700 <= gap <= 1300:
            continue
        try:
            short = nx.shortest_path(graph, a, b, weight="length")
            cheap = nx.shortest_path(graph, a, b, weight="_cost")
        except nx.NetworkXNoPath:
            continue
        if major_share(graph, short) < 0.5 or major_share(graph, cheap) > 0.05:
            continue
        extra = path_cost(graph, cheap, "length") / path_cost(graph, short, "length")
        if extra > 1.35:
            continue
        # A sidewalk beside the busy road is technically quiet but shows nothing:
        # most of the pick must run at least a block away from the shortest way.
        busy = node_coordinates(graph, short)
        apart = [min(distance_meters(point, other) for other in busy) >= 80
                 for point in node_coordinates(graph, cheap)]
        if sum(apart) < 0.6 * len(apart):
            continue
        saving = path_cost(graph, short, "_cost") - path_cost(graph, cheap, "_cost")
        if best is None or saving > best[0]:
            best = (saving, short, cheap)
    if best is None:
        raise SystemExit("no busy/quiet pair found near the centre")
    return best[1], best[2]


def costs_figure(graph, start: Coordinate) -> tuple[str, dict]:
    short, cheap = busy_and_quiet(graph, start)
    frame = fitted(node_coordinates(graph, short + cheap), 1140, 560, padding=1.45)
    body = streets(graph, frame, curves=True)

    def geometry(nodes):
        points = []
        for u, v in zip(nodes, nodes[1:]):
            edge = _edge_geometry_coordinates(graph, u, v)
            points += edge if not points else edge[1:]
        return points

    body += route_path(frame, geometry(short), RED, 5, dash="1 9")
    body += route_path(frame, geometry(cheap), GREEN_2, 5)
    for node, label in ((short[0], "A"), (short[-1], "B")):
        x, y = frame.xy(*(float(graph.nodes[node][k]) for k in ("y", "x")))
        body.append(f'<circle cx="{x:.0f}" cy="{y:.0f}" r="13" fill="{GREEN}"/>'
                    + text(x, y + 5, label, size=13, colour=PANEL, weight=700, anchor="middle", halo=False))
    signals = signal_events(graph, short)
    facts = {
        "short_km": path_cost(graph, short, "length") / 1000,
        "short_cost": path_cost(graph, short, "_cost") / 1000,
        "cheap_km": path_cost(graph, cheap, "length") / 1000,
        "cheap_cost": path_cost(graph, cheap, "_cost") / 1000,
        "short_major": major_share(graph, short),
        "signals": signals,
    }
    rows = [
        (RED, "Shortest", f"{facts['short_km']:.2f} km", f"costs {facts['short_cost']:.2f} km",
         f"{facts['short_major']:.0%} on a major road" + (f", {signals} signal{'s' * (signals != 1)}" if signals else "")),
        (GREEN_2, "RunLine's pick", f"{facts['cheap_km']:.2f} km", f"costs {facts['cheap_cost']:.2f} km", "quiet streets"),
    ]
    body.append(panel(24, 24, 470, 102))
    for index, (colour, name, km, cost, note) in enumerate(rows):
        y = 58 + 40 * index
        body.append(f'<rect x="40" y="{y - 7}" width="26" height="6" rx="3" fill="{colour}"/>')
        body.append(text(76, y, name, size=14, weight=600, halo=False))
        body.append(text(196, y, f"{km} · {cost}", size=14, halo=False))
        body.append(text(76, y + 18, note, size=12, colour=MUTED, halo=False))
    body.append(scale_bar(frame, 1140 - 40 - 200, 524))
    title = (f"Two ways from A to B: the shortest, {facts['short_km']:.2f} km mostly on a major road, costs "
             f"{facts['short_cost']:.2f} km; RunLine's pick, {facts['cheap_km']:.2f} km on quiet streets, costs {facts['cheap_cost']:.2f} km")
    return svg(1140, 560, title, body), facts


def funnel_figure(core, drawn, start: Coordinate) -> tuple[str, dict]:
    cell = cell_id(start.latitude, start.longitude, 200.0)
    pool = cell_pool(core, cell, tuple(range(1, 13)), SurfacePreference.MIXED)
    preferences = RoutePreferences(TARGET_MILES, surface=SurfacePreference.MIXED)
    loops = pool["loops"]
    candidates = [
        RouteCandidate(f"pool-{index}", tuple(loop["nodes"]), (), RouteMetrics(**loop["metrics"]))
        for index, loop in enumerate(loops)
    ]
    near = [c for c in candidates
            if abs(c.metrics.distance_miles - TARGET_MILES) <= preferences.distance_tolerance_miles]
    top = rank_candidates(list(candidates), preferences)[: preferences.result_count]
    every = [p for c in candidates for p in node_coordinates(core, c.node_ids)]
    size = 360
    frame = fitted(every, size, size, padding=1.04)
    background = streets(drawn, frame, curves=False)
    faint = "#cfcdc4"

    def lines(chosen, colour_for, width, opacity, longest_first=True):
        ordered = sorted(chosen, key=lambda c: -c.metrics.distance_miles if longest_first else 0)
        out = []
        for candidate in ordered:
            out += route_path(frame, node_coordinates(core, candidate.node_ids), colour_for(candidate), width,
                              casing=False, opacity=opacity)
        return out

    stages = [
        (f"All {len(candidates)} saved loops", lines(candidates, lambda c: distance_colour(c.metrics.distance_miles), 1.4, 0.75)),
        (f"{len(near)} within 5 ± 0.25 mi",
         lines(candidates, lambda c: faint, 1.0, 0.6) + lines(near, lambda c: GREEN_2, 2.2, 0.9)),
        (f"Top {len(top)} after ranking",
         lines(candidates, lambda c: faint, 1.0, 0.45)
         + [part for index, c in reversed(list(enumerate(top)))
            for part in route_path(frame, node_coordinates(core, c.node_ids), ROUTES[index], 3.6 if index == 0 else 2.8)]),
    ]
    # The street map is drawn once and reused by each panel.
    body = [f'<defs><g id="streets">{"".join(background)}</g></defs>']
    for index, (label, content) in enumerate(stages):
        x = index * (size + 30)
        body.append(
            f'<g transform="translate({x} 0)"><clipPath id="f{index}"><rect width="{size}" height="{size}" rx="16"/></clipPath>'
            f'<g clip-path="url(#f{index})"><rect width="{size}" height="{size}" fill="{CANVAS}"/>'
            + '<use href="#streets"/>' + "".join(content) + pin(frame, Coordinate(*pool["origin"]))
            + panel(12, 12, 9 + 8.2 * len(label), 32) + text(24, 33, label, size=14, weight=600, halo=False)
            + "</g></g>"
        )
        if index < 2:
            ax = x + size + 6
            body.append(f'<path d="M{ax} {size / 2 - 7} l12 7 -12 7z" fill="{MUTED}"/>')
    facts = {"loops": len(candidates), "near": len(near), "top": [round(c.metrics.distance_miles, 2) for c in top]}
    title = (f"From one start cell's saved pool to an answer: all {len(candidates)} loops, the {len(near)} within "
             f"a quarter mile of {TARGET_MILES} miles, and the top {len(top)} after ranking")
    width = 3 * size + 60
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {size}" role="img" aria-label="{title}">'
            + "".join(body) + "</svg>\n"), facts


ARCHITECTURE_TITLE = (
    "The browser loads runline-nine.vercel.app, whose /api rewrite reaches FastAPI on "
    "runline-capstone.vercel.app with the rebuilt Charlottesville graph and complete pool. "
    "On a coverage miss it forwards the request through RUNLINE_REMOTE_API to the Oracle VM, "
    "which serves Washington, DC, New York and Los Angeles from their tile graphs and pools."
)


def architecture_figure() -> str:
    """The deployed request chain, in the same card style as the map figures."""

    width, height, box_w = 1140, 432, 300
    columns = (28, 420, 812)
    style = (
        f'<defs><marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" '
        f'orient="auto-start-reverse"><path d="M0 0 L10 5 L0 10 z" fill="{MUTED}"/></marker><style>'
        f'text {{ font-family: "DM Sans", "Inter", -apple-system, "Segoe UI", sans-serif; fill: {INK}; }}'
        '.name { font-size: 17px; font-weight: 600; } .what { font-size: 13.5px; }'
        f'.tech {{ font-size: 12px; fill: {MUTED}; }} .edge {{ font-size: 12.5px; fill: {MUTED}; }}'
        f'.tag {{ font-size: 13px; font-weight: 600; fill: {GREEN_2}; }}</style></defs>'
    )

    def card(x: int, y: int, h: int, name: str, lines: list[str], tech: str, *, dashed: bool = False,
             w: int = box_w) -> list[str]:
        stroke = 'stroke="#8a8d84" stroke-dasharray="6 5" fill="none"' if dashed else f'stroke="{LINE}" fill="{PANEL}"'
        parts = [f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="14" stroke-width="1.5" {stroke}/>',
                 f'<text class="name" x="{x + 22}" y="{y + 32}">{name}</text>']
        parts += [f'<text class="what" x="{x + 22}" y="{y + 58 + 19 * i}">{line}</text>' for i, line in enumerate(lines)]
        parts.append(f'<text class="tech" x="{x + 22}" y="{y + h - 16}">{tech}</text>')
        return parts

    def arrow(d: str, colour: str = MUTED) -> str:
        return f'<path d="{d}" fill="none" stroke="{colour}" stroke-width="1.6" marker-end="url(#arrow)"/>'

    def edge(x: int, y: int, label: str, anchor: str = "middle") -> str:
        return f'<text class="edge" x="{x}" y="{y}" text-anchor="{anchor}">{label}</text>'

    top, row_h = 56, 148
    body = [style, f'<rect width="{width}" height="{height}" fill="{CANVAS}"/>',
            f'<text class="tag" x="28" y="38">Every request</text>']
    body += card(columns[0], top, row_h, "Browser", ["Map, start, distance,", "surface and hills;",
                                                    "GPX download"], "web/ · Leaflet")
    body += card(columns[1], top, row_h, "runline-nine.vercel.app", ["Canonical site: serves the page",
                                                                    "Rewrites /api/* to the",
                                                                    "capstone API (same origin)"],
                 "vercel.json · Git deployment")
    body += card(columns[2], top, row_h, "runline-capstone.vercel.app", ["FastAPI with the rebuilt",
                                                                        "Charlottesville graph + full pool",
                                                                        "Saved loops first, live on a miss"],
                 "scripts/release_capstone.py")
    for left, out_label, back_label in ((columns[0] + box_w, "page + /api", "routes + GPX"),
                                        (columns[1] + box_w, "/api/* rewrite", "routes + GPX")):
        right = left + 92
        body += [arrow(f"M{left} 104 H{right - 4}"), arrow(f"M{right} 160 H{left + 4}"),
                 edge(left + 46, 94, out_label), edge(left + 46, 182, back_label)]

    lower = top + row_h + 64
    body += card(columns[2], lower, 140, "Oracle VM regional API", ["FastAPI on main: DC, NYC, LA",
                                                                   "1,136 tile graphs + 12 GB pools"],
                 "Direct HTTPS URL · no forwarding")
    x_down, x_up = columns[2] + 80, columns[2] + 170
    body += [arrow(f"M{x_down} {top + row_h} V{lower - 4}"), arrow(f"M{x_up} {lower} V{top + row_h + 4}"),
             edge(x_down - 10, top + row_h + 28, "Coverage miss only:", "end"),
             edge(x_down - 10, top + row_h + 46, "RUNLINE_REMOTE_API", "end"),
             edge(x_up + 10, top + row_h + 37, "routes + GPX", "start")]
    body.append(f'<text class="tag" x="28" y="{lower + 4}" style="fill:{MUTED}">Built ahead of time</text>')
    body += card(28, lower + 20, 120, "Saved routes", [
        "Loops computed on the UVA CS cluster. Each pool ships beside its exact graph;",
        "a pool built for another graph is ignored and that start plans live.",
    ], "Cville data pinned in deploy/capstone-artifacts.json", dashed=True, w=columns[1] + box_w - 28)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" role="img" '
            f'aria-labelledby="title"><title id="title">{ARCHITECTURE_TITLE}</title>'
            f'<clipPath id="card"><rect width="{width}" height="{height}" rx="22"/></clipPath>'
            f'<g clip-path="url(#card)">' + "".join(body) + "</g></svg>\n")


FIGURES = ("architecture", "real-loops", "graph", "costs", "pool")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("figures", nargs="*", choices=FIGURES, help="figures to write (default: all)")
    selected = set(parser.parse_args(argv).figures or FIGURES)
    OUT.mkdir(parents=True, exist_ok=True)
    if "architecture" in selected:
        (OUT / "architecture.svg").write_text(architecture_figure())
        print(f"  architecture.svg {(OUT / 'architecture.svg').stat().st_size / 1024:.0f} KB")
        selected.discard("architecture")
    if not selected:
        return 0
    area = next(item for item in areas() if item.slug == "charlottesville-va")
    graph = load_area_graph(area)
    centre = graph.nodes[_nearest_node(graph, area.center)]
    start = Coordinate(float(centre["y"]), float(centre["x"]))
    # The planner's view: parallel OSM edges collapsed to one, with costs.
    drawn = collapse_graph(graph, RoutePreferences(TARGET_MILES, surface=SurfacePreference.MIXED))
    core = prepare_area(graph, SurfacePreference.MIXED)

    if "real-loops" in selected:
        loops, routes = loops_figure(core, drawn, start)
        (OUT / "real-loops.svg").write_text(loops)
        print("real-loops.svg:", ", ".join(f"{r.metrics.distance_miles:.2f} mi" for r in routes))
    if "graph" in selected:
        (OUT / "graph.svg").write_text(graph_figure(drawn, start))
    if "costs" in selected:
        costs, facts = costs_figure(drawn, start)
        (OUT / "costs.svg").write_text(costs)
        print("costs.svg:", {k: round(v, 3) for k, v in facts.items()})
    if "pool" in selected:
        funnel, counts = funnel_figure(core, drawn, start)
        (OUT / "pool.svg").write_text(funnel)
        print("pool.svg:", counts)
    for name in sorted(selected):
        print(f"  {name}.svg {(OUT / f'{name}.svg').stat().st_size / 1024:.0f} KB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
