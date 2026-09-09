# Runline

Deterministic running-loop generation over OpenStreetMap data. The prototype
optimizes for target distance and low interruption count, then lets the runner
choose road/trail mix and flat/balanced/hilly elevation. It exports GPX files for
use in a watch-compatible route app.

**Live app:** [runline-nine.vercel.app](https://runline-nine.vercel.app)

## Current milestone

- Generate multiple loop candidates from a start point.
- Penalize mapped traffic signals, major-road crossings, repeated segments, and
  distance error with visible score components.
- Prefer road, mixed, or trail surfaces.
- Prefer flat, balanced, or hilly candidates using cached Copernicus DEM
  elevation from Open-Meteo.
- Export GPX, GeoJSON, and a JSON score breakdown.
- Benchmark 3, 5, 8, and 12 mile routes in Charlottesville and Ashburn.

With `--drive-radius 1`, `3`, or `5`, the engine compares `start here` against
nearby mapped trailheads, publicly accessible parking, and parks. It calculates
driving distance over the OSM road network and includes the drive cost in the
same transparent route score.

## Privacy

Committed benchmark metadata contains labels and regions only. Exact residential
addresses live in `.private/benchmarks.local.json`, which is gitignored. Generated
routes, coordinates, map caches, and output files are also ignored.

## Setup and usage

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/runline generate \
  --address "Legacy Elementary School, Ashburn, Virginia" \
  --distance 5 \
  --surface mixed \
  --elevation balanced \
  --output output/legacy-5
```

The command writes `option-1.gpx`, `option-2.gpx`, `option-3.gpx`,
`routes.geojson`, `routes.json`, and a self-contained `preview.html` route
comparison (the street tiles require an internet connection).

## Map coverage and precomputed graphs

Route generation needs an OpenStreetMap walk graph for the area around the
start point. Downloading one from the Overpass API takes anywhere from ten
seconds to several minutes depending on how hard Overpass is throttling, which
is far longer than a serverless request may run. The deployed app therefore
never calls Overpass: it ships prebuilt graphs and serves only the areas it has.

Areas are declared in `config/areas.json` and built into `graphs/`:

```bash
.venv/bin/python scripts/build_graphs.py             # build anything missing
.venv/bin/python scripts/build_graphs.py --force     # rebuild everything
.venv/bin/python scripts/build_graphs.py --only richmond-va
```

The script writes one gzipped pickle per area plus `graphs/manifest.json`, all
committed so a deploy needs no network access. Pickle rather than GraphML
because a 40k-node graph loads in 0.9s against 6.3s, at a fifth of the peak
memory. `requirements.txt` pins exact versions so the graphs load under the
same library versions that built them.

**Choosing a radius.** Cost scales with node count, not area radius, so dense
cities need smaller radii. Measured on a full 5 mile request: 57k nodes peaks
near 490MB RSS, while 118k nodes peaks near 1186MB and will exhaust a 1GB
serverless function. The build script warns above 70k nodes; take the warning
seriously and lower `radius_meters` for that area rather than raising the
threshold. An area's radius also caps route length, since the whole route disc
must fit inside it.

Locally, an address outside every shipped area still falls back to a live
Overpass download and caches it under `cache/`. That fallback is disabled
whenever `VERCEL` is set, and can be forced either way with
`RUNLINE_ALLOW_OSM_DOWNLOAD=1` or `=0`. Uncovered requests return HTTP 422 with
a message naming the supported areas.

## Frontend

```bash
.venv/bin/runline-web
```

Open `http://127.0.0.1:8765`. The map-first interface can geocode an address,
accept a clicked or current location, generate routes with every engine
preference, compare options, and download the selected route as GPX.

## Known data limitations

Traffic-signal and surface metrics are only as complete as OpenStreetMap tags.
Unmapped stop signs and crossings cannot yet be counted. Open-Meteo elevation is
based on the 90 m Copernicus DEM GLO-90 dataset, so it describes terrain rather
than curb-level changes. Route output should be visually inspected before use
because map data can contain pedestrian-access errors.

Map data © OpenStreetMap contributors. Elevation data © Copernicus DEM 2021,
provided by Open-Meteo.

Elevation is fetched only for the strongest preliminary route candidates and is
cached by roughly 100 m cells. If the elevation service is unavailable, route
generation still succeeds and labels elevation as unavailable rather than zero.
