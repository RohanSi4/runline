"""Make a self-contained fallback demo from a captured, real API response.

The result is explicitly labeled as a saved example, supports route selection
and real GPX downloads, and draws streets from a trusted local OSM graph.
It needs neither the API nor map tiles. Keep generated files in output/.
"""

from __future__ import annotations

import argparse
import datetime as dt
import gzip
import json
import pickle
from pathlib import Path


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture", type=Path, required=True)
    parser.add_argument("--graph", type=Path, required=True, help="trusted, locally built .pkl.gz graph")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    payload = json.loads(args.capture.read_text())
    if not payload.get("routes"):
        parser.error("capture has no routes")
    points = [p for r in payload["routes"] for p in r["feature"]["geometry"]["coordinates"]]
    west, east = min(p[0] for p in points), max(p[0] for p in points)
    south, north = min(p[1] for p in points), max(p[1] for p in points)
    dx, dy = (east - west) * 0.1, (north - south) * 0.1
    bounds = [west - dx, south - dy, east + dx, north + dy]
    with gzip.open(args.graph, "rb") as stream:
        graph = pickle.load(stream)
    roads, seen = [], set()
    for u, v, data in graph.edges(data=True):
        key = (min(u, v), max(u, v))
        if key in seen:
            continue
        seen.add(key)
        geometry = data.get("geometry")
        coordinates = list(geometry.coords) if geometry is not None else [(graph.nodes[n]["x"], graph.nodes[n]["y"]) for n in (u, v)]
        if (max(p[0] for p in coordinates) < bounds[0] or min(p[0] for p in coordinates) > bounds[2]
                or max(p[1] for p in coordinates) < bounds[1] or min(p[1] for p in coordinates) > bounds[3]):
            continue
        kind = data.get("highway", "")
        kinds = {kind} if isinstance(kind, str) else set(kind)
        style = "major" if kinds & {"primary", "secondary", "tertiary", "trunk"} else (
            "trail" if kinds & {"path", "footway", "track", "bridleway"} else "street")
        roads.append([coordinates, style])
    data = {"routes": payload["routes"], "bounds": bounds, "roads": roads,
            "captured_at": dt.datetime.fromtimestamp(args.capture.stat().st_mtime, dt.timezone.utc).strftime("%b %d, %Y %H:%M UTC")}
    serialized = json.dumps(data, separators=(",", ":")).replace("<", "\\u003c")
    html = TEMPLATE.replace("__DATA__", serialized)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(html)
    print(f"Saved example: {len(payload['routes'])} routes, {len(roads)} street segments, "
          f"{args.output.stat().st_size / 1048576:.1f} MB; no network dependencies")
    return 0


TEMPLATE = '''<!doctype html>
<html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>RunLine · Saved demo</title>
<style>
*{box-sizing:border-box}body{margin:0;background:#f5f2ea;color:#252821;font:16px system-ui,sans-serif}
main{min-height:100vh;display:grid;grid-template-columns:360px 1fr}aside{padding:36px 28px;background:#fbfaf6;border-right:1px solid #ddd}
.brand{font-size:26px;font-weight:700;letter-spacing:-1px}.badge{display:inline-block;margin:22px 0 6px;padding:7px 10px;border-radius:20px;background:#e8eddf;color:#3d5937;font-size:12px}
h1{font-size:30px;line-height:1.13;letter-spacing:-1px}p{color:#666b61;line-height:1.55;font-size:14px}
.route{display:block;text-align:left;width:100%;background:#fff;border:1px solid #ddd;border-left:5px solid var(--color);border-radius:12px;padding:16px;margin:12px 0;cursor:pointer;color:inherit}
.route.active{outline:2px solid var(--color)}.route strong{display:block;font-size:20px;margin-bottom:7px}.route small{line-height:1.6;color:#656b60}
#download{margin-top:16px;padding:15px 20px;width:100%;border:0;border-radius:12px;background:#243f31;color:white;font:600 15px system-ui;cursor:pointer}
section{position:relative;min-height:70vh}svg{height:100%;width:100%;position:absolute;inset:0;background:#eeeee5}.street{fill:none;stroke:#d2d1c8;stroke-width:1.3}.major{fill:none;stroke:#c9b99e;stroke-width:2.4}.trail{fill:none;stroke:#c3cfb8;stroke-width:1.1}
.loop{fill:none;stroke-linejoin:round;stroke-linecap:round;cursor:pointer}.map-note{position:absolute;bottom:14px;left:20px;right:20px;font-size:11px;color:#60675e;background:#fbfaf6df;padding:10px 14px;border-radius:10px}
@media(max-width:700px){main{grid-template-columns:1fr}aside{padding:24px;border-right:0}section{height:65vh;min-height:450px}.brand{font-size:23px}h1{font-size:26px}.route{padding:12px;margin:10px 0}}
</style>
<main><aside><div class="brand">runline</div><span class="badge">Saved demo · works offline</span>
<h1>Your distance.<br>Fewer interruptions.</h1><p>This recorded example contains real routes from the running app. Choose a route to inspect it or download its GPX track.</p>
<div id="routes"></div><button id="download">Download selected GPX</button><p id="captured"></p>
<p>For fresh routes and new starting points, open the live RunLine app when you have a connection.</p></aside>
<section aria-label="Saved route map"><svg id="map" viewBox="0 0 1000 800" role="img" aria-label="OpenStreetMap street network and the saved running loops"></svg>
<div class="map-note">Map data © OpenStreetMap contributors (ODbL) · Terrain © Copernicus DEM GLO-90. Streets are embedded in this file; no map tiles are requested.</div></section></main>
<script>
const data=__DATA__;
const colors=['#e2552f','#235f9f','#7a4d96'];
const svg=document.getElementById('map');
const ns='http://www.w3.org/2000/svg';
const [west,south,east,north]=data.bounds;
const cosine=Math.cos((south+north)/2*Math.PI/180);
const width=(east-west)*cosine,height=north-south,scale=Math.min(900/width,680/height);
const x0=(1000-width*scale)/2,y0=(800-height*scale)/2;
function xy([lon,lat]){return [x0+(lon-west)*cosine*scale,y0+(north-lat)*scale]}
function path(points){return points.map((p,i)=>(i?'L':'M')+xy(p).map(v=>v.toFixed(2)).join(' ')).join(' ')}
function element(tag,attrs){const e=document.createElementNS(ns,tag);for(const [k,v] of Object.entries(attrs))e.setAttribute(k,v);svg.appendChild(e);return e}
for(const [points,kind] of data.roads)element('path',{d:path(points),class:kind});
const casing=element('path',{fill:'none',stroke:'#fff','stroke-width':11,'stroke-linejoin':'round','stroke-linecap':'round'});
const layers=data.routes.map((r,i)=>element('path',{d:path(r.feature.geometry.coordinates),class:'loop',stroke:colors[i]}));
let selected=0;
function select(index){selected=index;layers.forEach((p,i)=>{p.setAttribute('opacity',i===index?'1':'.25');p.setAttribute('stroke-width',i===index?'5':'3')});casing.setAttribute('d',layers[index].getAttribute('d'));svg.appendChild(casing);svg.appendChild(layers[index]);document.querySelectorAll('.route').forEach((b,i)=>{b.classList.toggle('active',i===index);b.setAttribute('aria-pressed',String(i===index))})}
data.routes.forEach((route,index)=>{const m=route.feature.properties;const b=document.createElement('button');b.className='route';b.style.setProperty('--color',colors[index]);const title=document.createElement('strong');title.textContent=`Route ${index+1} · ${m.distance_miles.toFixed(2)} mi`;const details=document.createElement('small');details.textContent=`${m.traffic_signal_events} signals · ${m.major_crossing_events} major crossings · ${Math.round(m.trail_fraction*100)}% trail`+(m.elevation_coverage>=.95?` · ${Math.round(m.elevation_gain_feet)} ft climb`:'');b.append(title,details);b.onclick=()=>select(index);document.getElementById('routes').appendChild(b);layers[index].onclick=()=>select(index)});
select(0);
const [sx,sy]=xy(data.routes[0].feature.geometry.coordinates[0]);element('circle',{cx:sx,cy:sy,r:7,fill:'#243f31',stroke:'#fff','stroke-width':3});
document.getElementById('captured').textContent='Captured '+data.captured_at+'. This example is saved, not a live request.';
document.getElementById('download').onclick=()=>{const a=document.createElement('a');const url=URL.createObjectURL(new Blob([data.routes[selected].gpx],{type:'application/gpx+xml'}));a.href=url;a.download=`runline-example-${selected+1}.gpx`;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000)};
</script></html>'''


if __name__ == "__main__":
    raise SystemExit(main())
