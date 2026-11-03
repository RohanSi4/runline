"""Render the README's map figures from real RunLine output.

    python scripts/readme_figures.py            # writes docs/readme/*.svg

Every figure is drawn from the shipped Charlottesville graph and the real
planner, starting at the area's map centre from graphs/manifest.json, so no
coordinates live in this file. SVG is written directly (no plotting library)
in the app's palette, with positions in local metres rounded to the pixel.
"""

from __future__ import annotations

import math
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))
os.environ.setdefault("RUNLINE_ALLOW_OSM_DOWNLOAD", "0")

from runline.coverage import areas, load_area_graph  # noqa: E402
from runline.grid import cell_id  # noqa: E402
from runline.models import Coordinate, RoutePreferences, SurfacePreference  # noqa: E402
from runline.osm import (  # noqa: E402
    _edge_geometry_coordinates,
    _is_major,
    _is_signal,
    _nearest_node,
    collapse_graph,
    generate_loops,
)
from scripts.precompute_pools import cell_pool, prepare_area  # noqa: E402

OUT = ROOT / "docs" / "readme"
CANVAS, PANEL, INK, MUTED, LINE = "#f4f1e9", "#fbfaf6", "#20221f", "#62655d", "#deddd6"
GREEN, GREEN_2, LIME, ORANGE, RED = "#193c2c", "#2f6348", "#c8e663", "#e2552f", "#a33225"
ROUTES = ("#e2552f", "#235f9f", "#7a4d96")
FONT = 'font-family="DM Sans, Inter, -apple-system, Segoe UI, sans-serif"'


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
        cells = [self.xy(p.latitude, p.longitude) for p in points]
        parts, last = [], None
        for index, (x, y) in enumerate(cells):
            here = (round(x), round(y))
            if here == last:
                continue
            parts.append(("M" if index == 0 else "L") + f"{here[0]} {here[1]}")
            last = here
        return "".join(parts)


def svg(width: int, height: int, title: str, body: list[str]) -> str:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" role="img" aria-label="{title}">'
        f'<clipPath id="card"><rect width="{width}" height="{height}" rx="22"/></clipPath>'
        f'<g clip-path="url(#card)"><rect width="{width}" height="{height}" fill="{CANVAS}"/>'
        + "".join(body)
        + "</g></svg>\n"
    )


def fitted(points, width: int, height: int, padding: float = 1.12) -> Frame:
    """A frame centred on the points' bounding box with all of them inside."""

    latitudes = [p.latitude for p in points]
    longitudes = [p.longitude for p in points]
    centre = Coordinate((min(latitudes) + max(latitudes)) / 2, (min(longitudes) + max(longitudes)) / 2)
    x_metres = 111_320.0 * math.cos(math.radians(centre.latitude))
    half_w = (max(longitudes) - min(longitudes)) * x_metres / 2
    half_h = (max(latitudes) - min(latitudes)) * 110_540.0 / 2
    return Frame(centre, max(half_w, half_h * width / height) * padding, width, height)


def key_panel(x: float, y: float, width: float, height: float) -> str:
    return (f'<rect x="{x}" y="{y}" width="{width}" height="{height}" rx="10" fill="{PANEL}" '
            f'fill-opacity="0.94" stroke="{LINE}"/>')


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
        points = (
            _edge_geometry_coordinates(graph, u, v)
            if curves
            else [Coordinate(float(graph.nodes[n]["y"]), float(graph.nodes[n]["x"])) for n in (u, v)]
        )
        (major_d if _is_major(data) else minor_d).append(frame.path(points))
    return [
        f'<path d="{"".join(minor_d)}" fill="none" stroke="{minor}" stroke-width="1.2" stroke-linecap="round"/>',
        f'<path d="{"".join(major_d)}" fill="none" stroke="{major}" stroke-width="3.2" stroke-linecap="round"/>',
    ]


def route_path(frame: Frame, points, colour: str, width: float, casing: bool = True, opacity: float = 1) -> list[str]:
    d = frame.path(points)
    out = []
    if casing:
        out.append(f'<path d="{d}" fill="none" stroke="{PANEL}" stroke-width="{width + 4.5}" '
                   'stroke-linejoin="round" stroke-linecap="round"/>')
    out.append(f'<path d="{d}" fill="none" stroke="{colour}" stroke-width="{width}" '
               f'stroke-linejoin="round" stroke-linecap="round" opacity="{opacity}"/>')
    return out


def pin(frame: Frame, at: Coordinate) -> str:
    x, y = frame.xy(at.latitude, at.longitude)
    return (f'<g transform="translate({x:.0f} {y:.0f})"><circle r="11" fill="{GREEN}"/>'
            f'<circle r="7.5" fill="none" stroke="{LIME}" stroke-width="1.8"/><circle r="3.2" fill="{LIME}"/></g>')


def node_coordinates(graph, nodes):
    return [Coordinate(float(graph.nodes[n]["y"]), float(graph.nodes[n]["x"])) for n in nodes]


def loops_figure(graph, drawn, start: Coordinate) -> tuple[str, list]:
    core = prepare_area(graph, SurfacePreference.MIXED)
    routes = generate_loops(core, start, RoutePreferences(5, surface=SurfacePreference.MIXED))
    frame = fitted([point for route in routes for point in route.coordinates], 1140, 640)
    body = streets(drawn, frame, curves=True)
    for index in reversed(range(len(routes))):
        body += route_path(frame, routes[index].coordinates, ROUTES[index], 5 if index == 0 else 3.5,
                           opacity=1 if index == 0 else 0.85)
    body.append(pin(frame, start))
    title = "The three 5 mile loops RunLine returns from the Charlottesville map centre, drawn on the shipped walk graph"
    return svg(1140, 640, title, body), routes


def mix(a: str, b: str, t: float) -> str:
    pa = [int(a[i : i + 2], 16) for i in (1, 3, 5)]
    pb = [int(b[i : i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x + (y - x) * t):02x}" for x, y in zip(pa, pb))


def distance_colour(miles: float) -> str:
    t = max(0.0, min(1.0, (miles - 1) / 11))
    return mix("#a9cf3b", GREEN_2, t / 0.5) if t < 0.5 else mix(GREEN_2, GREEN, (t - 0.5) / 0.5)


def pool_figure(graph, drawn, start: Coordinate) -> tuple[str, int]:
    core = prepare_area(graph, SurfacePreference.MIXED)
    cell = cell_id(start.latitude, start.longitude, 200.0)
    pool = cell_pool(core, cell, tuple(range(1, 13)), SurfacePreference.MIXED)
    loops = sorted(pool["loops"], key=lambda loop: -loop["metrics"]["distance_miles"])
    origin = Coordinate(*pool["origin"])
    frame = fitted([p for loop in loops for p in node_coordinates(core, loop["nodes"])], 1140, 760, padding=1.06)
    body = streets(drawn, frame, curves=False)
    for loop in loops:
        points = node_coordinates(core, loop["nodes"])
        body += route_path(frame, points, distance_colour(loop["metrics"]["distance_miles"]), 1.6,
                           casing=False, opacity=0.75)
    body.append(pin(frame, origin))
    # distance key
    stops = "".join(f'<stop offset="{t / 10}" stop-color="{distance_colour(1 + 11 * t / 10)}"/>' for t in range(11))
    body.append(key_panel(24, 672, 252, 64))
    body.append(f'<defs><linearGradient id="d">{stops}</linearGradient></defs>'
                f'<rect x="40" y="712" width="220" height="8" rx="4" fill="url(#d)"/>'
                f'<text x="40" y="700" {FONT} font-size="13" fill="{INK}">1 mi</text>'
                f'<text x="260" y="700" {FONT} font-size="13" fill="{INK}" text-anchor="end">12 mi</text>')
    title = f"Every distinct loop in one start cell's precomputed pool, {len(loops)} loops from 1 to 12 miles, coloured by distance"
    return svg(1140, 760, title, body), len(loops)


def graph_figure(graph, start: Coordinate) -> str:
    """The same few blocks as mapped (every shape point) and as searched (intersections)."""

    width, height, half = 540, 400, 130
    panels = []
    for offset, searched in ((0, False), (600, True)):
        frame = Frame(start, half, width, height)
        lines, dots, seen = [], [], set()
        nodes = set()
        for u, v, data in graph.edges(data=True):
            key = (min(u, v), max(u, v))
            if key in seen:
                continue
            seen.add(key)
            ends = [frame.xy(float(graph.nodes[n]["y"]), float(graph.nodes[n]["x"])) for n in (u, v)]
            if not any(frame.inside(*end, margin=0) for end in ends):
                continue
            nodes.update((u, v))
            if searched:
                lines.append(frame.path(node_coordinates(graph, (u, v))))
            else:
                points = _edge_geometry_coordinates(graph, u, v)
                lines.append(frame.path(points))
                for point in points[1:-1]:
                    x, y = frame.xy(point.latitude, point.longitude)
                    if frame.inside(x, y, margin=0):
                        dots.append(f'<circle cx="{x:.0f}" cy="{y:.0f}" r="3.2" fill="{PANEL}" stroke="{MUTED}" stroke-width="1.3"/>')
        for n in nodes:
            x, y = frame.xy(float(graph.nodes[n]["y"]), float(graph.nodes[n]["x"]))
            if frame.inside(x, y, margin=0):
                dots.append(f'<circle cx="{x:.0f}" cy="{y:.0f}" r="4.6" fill="{GREEN}"/>')
        panels.append(
            f'<g transform="translate({offset} 0)"><clipPath id="c{offset}"><rect width="{width}" height="{height}" rx="16"/></clipPath>'
            f'<rect width="{width}" height="{height}" rx="16" fill="{PANEL}" stroke="{LINE}"/>'
            f'<g clip-path="url(#c{offset})"><path d="{"".join(lines)}" fill="none" stroke="{MUTED if searched else "#9a9c94"}" '
            f'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/>{"".join(dots)}</g></g>'
        )
    title = "The same blocks as mapped, with every shape point, and as the planner searches them, intersections only"
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1140 400" role="img" aria-label="{title}">'
            + "".join(panels) + "</svg>\n")


def cost_colour(factor: float) -> str:
    if factor <= 1.05:
        return GREEN_2
    if factor < 2:
        return mix("#c9a227", ORANGE, (factor - 1.05) / 0.95)
    return mix(ORANGE, RED, min(1.0, (factor - 2) / 2))


def costs_figure(collapsed, start: Coordinate) -> str:
    frame = Frame(start, 1_500, 1140, 640)
    groups: dict[str, list[str]] = {}
    seen = set()
    for u, v, data in collapsed.edges(data=True):
        key = (min(u, v), max(u, v))
        if key in seen:
            continue
        seen.add(key)
        ends = [frame.xy(float(collapsed.nodes[n]["y"]), float(collapsed.nodes[n]["x"])) for n in (u, v)]
        if not any(frame.inside(*end, margin=200) for end in ends):
            continue
        length = float(data.get("length", 0)) or 1.0
        reverse = collapsed.get_edge_data(v, u) or data
        factor = max(float(data["_cost"]), float(reverse["_cost"])) / length
        colour = cost_colour(factor)
        groups.setdefault(colour, []).append(frame.path(_edge_geometry_coordinates(collapsed, u, v)))
    body = []
    for colour in sorted(groups, key=lambda c: c != GREEN_2):
        width = 1.6 if colour == GREEN_2 else 2.6
        body.append(f'<path d="{"".join(groups[colour])}" fill="none" stroke="{colour}" stroke-width="{width}" '
                    'stroke-linecap="round" stroke-linejoin="round"/>')
    for node, data in collapsed.nodes(data=True):
        if _is_signal(data):
            x, y = frame.xy(float(data["y"]), float(data["x"]))
            if frame.inside(x, y, margin=0):
                body.append(f'<circle cx="{x:.0f}" cy="{y:.0f}" r="4" fill="{RED}" stroke="{PANEL}" stroke-width="1.5"/>')
    key = [("1×", GREEN_2), ("1.5×", cost_colour(1.5)), ("2×", cost_colour(2)), ("3×+", RED)]
    body.append(key_panel(24, 580, 500, 42))
    x = 40
    for label, colour in key:
        body.append(f'<rect x="{x}" y="598" width="26" height="6" rx="3" fill="{colour}"/>'
                    f'<text x="{x + 32}" y="605" {FONT} font-size="13" fill="{INK}">{label}</text>')
        x += 92
    body.append(f'<circle cx="{x + 6}" cy="601" r="4" fill="{RED}"/>'
                f'<text x="{x + 16}" y="605" {FONT} font-size="13" fill="{INK}">traffic signal</text>')
    title = "Runner cost of every street near the map centre: green costs its length, orange and red are major roads and signal approaches the planner avoids"
    return svg(1140, 640, title, body)


def main() -> int:
    area = next(item for item in areas() if item.slug == "charlottesville-va")
    graph = load_area_graph(area)
    start = Coordinate(*_coordinates(graph, _nearest_node(graph, area.center)))
    OUT.mkdir(parents=True, exist_ok=True)
    # The planner's view: parallel OSM edges collapsed to one, with costs.
    drawn = collapse_graph(graph, RoutePreferences(5, surface=SurfacePreference.MIXED))

    loops, routes = loops_figure(graph, drawn, start)
    (OUT / "real-loops.svg").write_text(loops)
    print("real-loops.svg:", ", ".join(f"{r.metrics.distance_miles:.2f} mi" for r in routes))
    pool, count = pool_figure(graph, drawn, start)
    (OUT / "pool.svg").write_text(pool)
    print(f"pool.svg: {count} loops")
    (OUT / "graph.svg").write_text(graph_figure(drawn, start))
    (OUT / "costs.svg").write_text(costs_figure(drawn, start))
    for name in ("real-loops", "pool", "graph", "costs"):
        print(f"  {name}.svg {(OUT / f'{name}.svg').stat().st_size / 1024:.0f} KB")
    return 0


def _coordinates(graph, node) -> tuple[float, float]:
    return float(graph.nodes[node]["y"]), float(graph.nodes[node]["x"])


if __name__ == "__main__":
    raise SystemExit(main())
