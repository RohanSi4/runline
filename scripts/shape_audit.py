"""Audit route shape on the Charlottesville benchmark starts.

For the top three routes at the Rotunda, the map centre and six points 2.5 km
out, at 3/5/8/12 miles, reports how many are slivers (compactness below 0.2
or 0.35), touch a dead end, revisit a node within 600 m, or repeat over 10%
of their length. Run it after any change to generation or ranking:

    .venv/bin/python scripts/shape_audit.py

Reference on 768f149: 96 routes, 8 below 0.2, 0 dead ends, 0 short revisits.
"""

import math
import os
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root / "src"))
os.environ["RUNLINE_ALLOW_OSM_DOWNLOAD"] = "0"

from runline.models import Coordinate, RoutePreferences
from runline.osm import prepare_core, graph_radius_meters, generate_loops
from runline.scoring import rank_candidates

def dist(g, a, b):
    ya, xa, yb, xb = g.nodes[a]["y"], g.nodes[a]["x"], g.nodes[b]["y"], g.nodes[b]["x"]
    return math.hypot((xa - xb) * 111320 * math.cos(math.radians(ya)), (ya - yb) * 110540)

origins = {"rotunda": (38.035556, -78.503333), "centre": (38.029306, -78.4766781)}
for k in range(6):
    a = math.radians(k * 60)
    origins[f"ring_{k*60:03d}"] = (38.029306 + 2500 * math.cos(a) / 110540,
                                   -78.4766781 + 2500 * math.sin(a) / (111320 * math.cos(math.radians(38.03))))

rows = []
for name, (lat, lon) in origins.items():
    for miles in (3, 5, 8, 12):
        prefs = RoutePreferences(miles)
        o = Coordinate(lat, lon)
        g = prepare_core(o, graph_radius_meters(prefs), root / "cache", prefs)
        und = g.to_undirected(as_view=True)
        top = rank_candidates(generate_loops(g, o, prefs), prefs)[:3]
        for i, c in enumerate(top):
            nodes = list(c.node_ids)
            dead = sum(1 for n in nodes[1:-1] if und.degree(n) == 1)
            # lollipop: a node revisited after < 600 m of travel (cul-de-sac bulb, block lap)
            seen, travelled, lolli = {}, 0.0, 0
            for j, n in enumerate(nodes):
                if j:
                    travelled += dist(g, nodes[j - 1], n)
                if n in seen and travelled - seen[n] < 600 and j != len(nodes) - 1:
                    lolli += 1
                seen[n] = travelled
            rows.append((name, miles, i + 1, c.metrics.compactness, c.metrics.repeated_fraction, dead, lolli))

print(f"{len(rows)} routes")
print("compactness <0.35:", sum(r[3] < 0.35 for r in rows), " <0.2:", sum(r[3] < 0.2 for r in rows))
print("median compactness:", sorted(r[3] for r in rows)[len(rows) // 2])
print("routes touching a dead-end node:", sum(r[5] > 0 for r in rows))
print("routes with a short revisit (<600 m loop-back):", sum(r[6] > 0 for r in rows))
print("repeated >10%:", sum(r[4] > 0.10 for r in rows))
print("\nworst by compactness:")
for r in sorted(rows, key=lambda r: r[3])[:8]:
    print("  %-9s %2dmi #%d compact=%.2f repeat=%.0f%% deadends=%d revisits=%d" % (r[0], r[1], r[2], r[3], r[4] * 100, r[5], r[6]))
