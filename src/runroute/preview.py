from __future__ import annotations

import json


def preview_html(feature_collection: dict) -> str:
    route_data = json.dumps(feature_collection, separators=(",", ":")).replace(
        "</", "<\\/"
    )
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Run Route Preview</title>
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
  <style>
    :root {{ color-scheme: light dark; --bg: #f7f7f4; --panel: #ffffff; --text: #171717; --muted: #666; --border: #d9d9d4; --active: #ea580c; --route-1: #ea580c; --route-2: #2563eb; --route-3: #16a34a; --route-4: #9333ea; --route-5: #db2777; }}
    @media (prefers-color-scheme: dark) {{ :root {{ --bg: #151515; --panel: #202020; --text: #f5f5f5; --muted: #aaa; --border: #3b3b3b; --active: #fb923c; --route-1: #fb923c; --route-2: #60a5fa; --route-3: #4ade80; --route-4: #c084fc; --route-5: #f472b6; }} }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; font-family: ui-sans-serif, system-ui, sans-serif; color: var(--text); background: var(--bg); }}
    main {{ display: grid; grid-template-columns: minmax(250px, 330px) 1fr; min-height: 100vh; }}
    aside {{ padding: 18px; background: var(--panel); border-right: 1px solid var(--border); }}
    h1 {{ margin: 0 0 4px; font-size: 20px; }}
    .sub {{ margin: 0 0 16px; color: var(--muted); font-size: 13px; }}
    #route-list {{ display: grid; gap: 10px; }}
    button {{ width: 100%; padding: 12px; color: inherit; text-align: left; background: transparent; border: 1px solid var(--border); border-radius: 10px; cursor: pointer; }}
    button[aria-pressed="true"] {{ border-color: var(--active); box-shadow: inset 3px 0 0 var(--active); }}
    .option {{ display: flex; justify-content: space-between; gap: 8px; font-weight: 650; }}
    .metrics {{ margin-top: 6px; color: var(--muted); font-size: 12px; line-height: 1.5; }}
    #map {{ min-height: 100vh; }}
    .leaflet-container {{ background: var(--bg); }}
    @media (max-width: 700px) {{ main {{ grid-template-columns: 1fr; }} aside {{ border-right: 0; border-bottom: 1px solid var(--border); }} #map {{ min-height: 65vh; }} }}
  </style>
</head>
<body>
<main>
  <aside>
    <h1>Run options</h1>
    <p class="sub">Select a route to compare the actual tradeoffs.</p>
    <div id="route-list"></div>
  </aside>
  <div id="map" aria-label="Map of generated running routes"></div>
</main>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
const collection = {route_data};
const map = L.map('map');
L.tileLayer('https://tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{
  maxZoom: 19,
  attribution: '&copy; OpenStreetMap contributors'
}}).addTo(map);
const styles = getComputedStyle(document.documentElement);
const colors = [1, 2, 3, 4, 5].map(index => styles.getPropertyValue(`--route-${{index}}`).trim());
const layers = collection.features.map((feature, index) => {{
  const latLngs = feature.geometry.coordinates.map(([longitude, latitude]) => [latitude, longitude]);
  const layer = L.polyline(latLngs, {{ color: colors[index % colors.length], weight: 4, opacity: index === 0 ? 1 : 0.48 }}).addTo(map);
  return {{ feature, layer, latLngs }};
}});
const allBounds = L.latLngBounds(layers.flatMap(item => item.latLngs));
map.fitBounds(allBounds.pad(0.08));

function selectRoute(selected) {{
  layers.forEach((item, index) => {{
    item.layer.setStyle({{ opacity: index === selected ? 1 : 0.25, weight: index === selected ? 5 : 3 }});
    if (index === selected) item.layer.bringToFront();
  }});
  document.querySelectorAll('#route-list button').forEach((button, index) => button.setAttribute('aria-pressed', String(index === selected)));
  map.fitBounds(layers[selected].layer.getBounds().pad(0.12));
}}

const routeList = document.getElementById('route-list');
layers.forEach((item, index) => {{
  const p = item.feature.properties;
  const m = p;
  const start = p.start || {{ label: 'Start here' }};
  const elevation = m.elevation_coverage >= 0.95 ? `${{Math.round(m.elevation_gain_feet)}} ft gain` : 'elevation unavailable';
  const button = document.createElement('button');
  button.type = 'button';
  button.setAttribute('aria-pressed', String(index === 0));
  const heading = document.createElement('div');
  heading.className = 'option';
  const optionName = document.createElement('span');
  optionName.textContent = `Option ${{index + 1}}`;
  const distance = document.createElement('span');
  distance.textContent = `${{m.distance_miles.toFixed(2)}} mi`;
  heading.append(optionName, distance);
  const metrics = document.createElement('div');
  metrics.className = 'metrics';
  const startLine = document.createElement('div');
  startLine.textContent = `${{start.label}} · ${{m.drive_distance_miles.toFixed(1)}} mi drive`;
  const routeLine = document.createElement('div');
  routeLine.textContent = `${{elevation}} · ${{m.traffic_signal_events}} signals · ${{Math.round(m.trail_fraction * 100)}}% trail · ${{Math.round(m.repeated_fraction * 100)}}% repeated`;
  metrics.append(startLine, routeLine);
  button.append(heading, metrics);
  button.addEventListener('click', () => selectRoute(index));
  routeList.appendChild(button);
}});
</script>
</body>
</html>
"""
