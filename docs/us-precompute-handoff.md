# US route-pool precompute: handoff

Status as of 2026-11-02. Owner of the work so far: Krish. This file is the
single source of truth for taking route pools from Charlottesville to the
rest of the US. Update the **Status** and **Measured** sections as you go, so
the next person (or Claude session) starts from what is true.

## Goal

Answer most route requests in about 0.1 s by looking up loops computed
ahead of time on the UVA CS SLURM cluster, instead of generating them live
(0.7-2.4 s today on a laptop, more on Vercel). Start with the top metros, then
fill in the rest of the country lazily from live requests.

## How pools work

- The world is cut into a global grid of 200 m cells
  (`cell_id()` in `scripts/precompute_pools.py`). Ids look like
  `200:<row>:<column>` and are the same for every area, so the server can
  compute a request's cell from its start point alone.
- For each cell with a walkable node, and for each surface (road, mixed,
  trail), the planner runs at whole-mile targets 1-15 with
  `generate_loops(..., pool_size=20)` and every distinct loop is kept with its
  metrics. That is a **pool**.
- Distance and elevation preference only affect ranking
  (`rank_candidates`), so a request is answered by filtering a pool to the
  distance band and ranking it. Surface changes edge costs, so it gets its own
  pool.
- Drive-radius requests reuse pools: drive starts (parking, parks,
  trailheads in `graphs/<slug>-starts.json`) are just more cells.
- Shards are `pools/<area>/shard-NNNNN.jsonl.gz` (first line is a header
  with graph hash and commit). `scripts/merge_pools.py` writes
  `pools/<area>.sqlite`, one gzipped JSON row per (cell, surface), and refuses
  to mix shards from different graphs or commits.

## Status

Branch `route-shape-penalty` on `RohanSi4/runline` (not merged to `main`;
local and remote `main` have diverged by 16 commits each, so rebase onto
`origin/main` before opening a PR).

Done:
- Spur trimming and sliver handling in generation (`fcdcb35`, `5a7ac02`).
- scipy Dijkstra in `osm.py` (`_CostMatrix`): same routes on all 32
  benchmark cases, 1.6-1.9x faster end to end.
- Elevation baked from Copernicus GLO-90 tiles (`enrich_graph_copernicus`,
  `build_graphs.py --bake-only`); shipped Charlottesville graph is baked.
- `scripts/precompute_pools.py`, `scripts/merge_pools.py`, `slurm/`.
- Cluster setup works (`slurm/setup.sh`, uv-managed Python 3.12).

- **A (code):** `src/runline/tiling.py`, `scripts/build_metros.py`,
  `tests/test_tiling.py` on branch `metro-tiling`. The cell grid moved to
  `src/runline/grid.py` (same ids; `precompute_pools.py` re-exports it).
- **A (data):** `config/metros.json` generated from 2020 Census urban areas:
  top 50 metros, 8,677 tiles, 3,347,052 unique cells at 200 m.

In progress:
- Charlottesville pilot. Single-shard test is job 6788169 on Krish's account.
  Next: full 400-task array and merge, then fill in **Measured** below.

Not started: everything else under **Remaining work**.

## Measured

On Krish's M-series laptop, commit `768f149`, 32-case benchmark
(`scripts/bench.py`), cold, one repeat:

| Miles | End to end (median) | In band (of 24) |
| ---: | ---: | ---: |
| 3 | 0.67 s | 24 |
| 5 | 1.23 s | 24 |
| 8 | 1.87 s | 24 |
| 12 | 2.38 s | 19 |

Shape audit (`scripts/shape_audit.py`): 96 routes, 8 below 0.2 compactness,
0 dead ends, 0 short revisits, 8 repeating over 10%.

Profile of one 8 mile `generate_loops` after scipy: 1.17 s, of which
`measure_route` is 0.74 s (63%). That is the next hotspot.

Precompute, 3 cells locally: about 9 s per cell per surface, 27 s per cell
for all three surfaces. Pool coverage: dense cells hit about 17 of the 29
half-mile bands from 1-15 mi; sparse edge cells 3-8 (live generation finds
the same few loops there, so this is the map, not the pool).

Cluster pilot (fill in after the full Charlottesville run):

| Metric | Value |
| --- | --- |
| Seconds per cell (3 surfaces) | TBD |
| Array wall time, 400 tasks | TBD |
| MaxRSS per task | TBD |
| `charlottesville-va.sqlite` size | TBD |
| Slowest / median shard time | TBD |

## Running on the UVA CS cluster

Each teammate needs their own CS account (ask cshelpdesk). Everything runs
from a portal node; compute nodes are reached only through SLURM.

```bash
# from your laptop, in the repo
rsync -av --exclude .venv --exclude cache --exclude pools --exclude build-cache \
    ./ <computing-id>@portal.cs.virginia.edu:~/runline/
# on portal
cd ~/runline && bash slurm/setup.sh
sbatch --array=0 --export=AREA=charlottesville-va slurm/precompute.sbatch   # one-shard test
jid=$(sbatch --parsable --export=AREA=charlottesville-va slurm/precompute.sbatch)
sbatch --dependency=afterok:$jid --export=AREA=charlottesville-va slurm/merge.sbatch
squeue -u $USER
sacct -j $jid --format=JobID,Elapsed,MaxRSS,State
```

Things that already bit us:
- **Do not use `module load python`.** On portal09 the `python/3.12.3`
  module (needs `gcc/11.4.0` first) loads without error, but its directory
  under `/sw/ubuntu2204/ebu082025/...` does not exist, and Ubuntu's
  `python3` is 3.10. `setup.sh` installs uv into `~/.local/bin` and a
  standalone Python 3.12 instead; jobs just run `.venv/bin/python`.
- Shards that exist are skipped. Delete a test shard before the full run, and
  delete all shards after any code or graph change (merge refuses mixed
  builds anyway).
- `slurm/logs/` must exist before submitting; `setup.sh` creates it.
- Pickled graphs only load under the pinned versions in `requirements.txt`.
- Partitions (per user, concurrent): `cpu` 400 cores / 4 days,
  `gpu` 400 cores, `nolim` 80 cores / 20 days. QOS `csresnolim` exists but
  has the same priority. No GPU is needed for any of this.
- Home quota is 100 GB. Use `/bigtemp` (shared NFS, volatile) for the US
  extract, DEM tiles and shards; copy finished pools somewhere permanent.
- Tell cshelpdesk before a run that holds hundreds of cores for hours.

## Remaining work for the US

Ordered by dependency. Items in the same step can run in parallel; each
should be its own branch and PR off `route-shape-penalty` (or `main` once it
lands).

### Step 1 (parallel)

**A. Metro list and tiling.** Pick metros (proposal: top 50 by population,
boundaries from Census urban-area shapefiles). Today an area is one disc with
a 70k-node warning, sized for a 1 GB Vercel function. A 12 mi loop needs a
disc of about 10.6 km (`graph_radius_meters`), which in a dense city is far
over 70k nodes, so each metro becomes overlapping tiles such that every
cell's needed disc fits in one tile. Output: a config generator for
`config/areas.json` (or a new `config/metros.json`) and a test that every
cell of a metro is covered by some tile at the 12 mi radius.

*Done (code), branch `metro-tiling`.* How it works:
- A metro's cells are the 200 m grid cells touching its boundary (Census
  polygon, holes filled so interior parks count, grown 150 m, simplified).
- Tiles are discs of `radius = needed + cell half-diagonal + core`, where
  `needed = graph_radius_meters(12 mi)` = 10,622 m and `core` defaults to
  3 km, so tiles are 13.8 km. A tile **owns** cells within its `reach`
  (`radius - needed - half-diagonal`, about 3.0 km), which guarantees the
  cell's whole 12 mi disc is inside the tile. Centres are a hex lattice,
  every cell is then checked with haversine, gaps get an extra tile, and
  tiles owning nothing are dropped.
- Each cell's owner is the nearest tile centre among tiles that reach it
  (`tiling.assign_cells`). **D should group cells with exactly this rule**,
  over all tiles in `config/metros.json` (adjacent metros' tiles overlap, so
  run it over every metro's tiles at once and each cell gets one owner).
- Tile entries have the keys of a `config/areas.json` entry (slug, label,
  latitude, longitude, radius_meters) plus `reach_meters` and `cells`, so B
  can build a tile like any area. `config/areas.json` is untouched; it is
  still what ships to Vercel.
- `tests/test_tiling.py` checks synthetic metros (concave, islands, a
  200 m sliver, 25-61 degrees N) including a brute-force check independent
  of the vectorised code, and re-verifies the committed config. The needed
  radius comes from `graph_radius_meters`, so raising it fails the test until
  tiles are regenerated.

To generate the config (Census servers are reachable from a laptop, not
from every sandbox):
1. Boundaries: census.gov TIGER/Line Shapefiles, any year 2023 or later,
   layer *Urban Areas*, file `tl_<year>_us_uac20.zip` (2020 urban areas;
   older years carry the 2010 ones). Leave it zipped.
2. Populations, any one of:
   `https://api.census.gov/data/2020/dec/dhc?get=NAME,P1_001N&for=urban%20area:*`
   saved as `.json`; or a `.csv` with a population column and a GEOID or
   name column; or the Federal Register list of 2020 urban areas
   (FR doc 2022-28286 on govinfo.gov) saved as `.txt` (joins by name).
3. ```bash
   mkdir -p build-cache/census   # gitignored; put both downloads here
   .venv/bin/python scripts/build_metros.py \
       --urban-areas build-cache/census/tl_2024_us_uac20.zip \
       --population build-cache/census/ua_population.json --top 50
   .venv/bin/python scripts/build_metros.py --check
   ```
   It prints per-metro cells and tiles and the total unique cells (replace
   the 1.2M estimate in H with it). Puerto Rico and other territories are
   skipped because the Geofabrik US extract does not include them
   (`--include-territories` to keep them).

Measured on synthetic shapes: a NYC-sized 4,800 km^2 metro is 120k cells
and 521 / 246 / 98 tiles at a 2 / 3 / 5 km core, in under a second.

Found while doing A, for whoever picks these up:
- **Pools go to 15 mi but tiles are sized for 12.** `DEFAULT_TARGETS` is
  1-15, and a 15 mi crop is 13.3 km. A 15 mi loop from a cell at the edge of
  a tile's reach can be clipped. Either cap tile pools at 12 mi or run with
  `--max-miles 15` (tiles grow to 16.5 km). Decide before F.
- **Dense tiles will be big.** Tile size is dominated by the 10.6 km needed
  radius, not the core, so even a minimal tile is about 370 km^2. With
  sidewalks mapped separately, a Manhattan-like tile could reach 1M+ walk
  nodes; at today's ~8 MB per 1,000 nodes that is too much memory per
  SLURM task. B should report node counts per tile; D may want to crop each
  target to its own `graph_radius_meters` instead of preparing the whole
  tile once.
- The core width trades tile count (graph builds, storage) against tile
  size (Dijkstra time per call, memory). 3 km is a starting point; revisit
  with F's seconds-per-cell numbers.

**B. Graph builder from the Geofabrik US extract.** `build_graphs.py` uses
Overpass, which cannot serve the US. Read `us-latest.osm.pbf` (about 11 GB),
cut each tile with osmium, build the walk graph, drive graph, start
candidates and places with the same post-processing as today
(`_routable_core`, `build_start_candidates`, `build_place_index`), and bake
elevation (DEM tiles download on demand into `build-cache/dem/`). Acceptance:
rebuilding Charlottesville from the PBF gives a graph whose benchmark and
shape audit match the Overpass build within noise.

**C. `measure_route` speedup.** 63% of generation after scipy. Precompute
per-edge length, signal, surface and elevation arrays on the cost matrix and
sum with numpy; keep `_major_crossings` exact. Acceptance: identical metrics
on the 32 benchmark cases, faster profile.

### Step 2 (after A)

**D. Per-tile preparation in the precompute.** `precompute_pools.py`
prepares one core for the whole area. scipy's Dijkstra searches the whole
graph on every call (about 3 ms at 38k nodes, roughly 50 ms at 500k), so
cells must run against their tile's graph, not a metro-sized one. Group cells
by tile and give each SLURM task one tile's cells.

**E. Pool format v2.** Pools store OSM node ids, so serving needs the graph.
US graphs will not fit in Vercel's bundle, so store an encoded polyline (and
the metrics) per loop and bump `POOL_FORMAT_VERSION`. Decide this before any
metro-scale run; changing it later means recomputing.

### Step 3 (after D, E)

**F. Pilot metro.** One dense metro end to end (DC or the Ashburn area),
including the benchmark and shape audit on its starts, since shape tuning was
only done on Charlottesville. Re-measure seconds per cell on tile graphs; the
US estimate depends on it.

**G. Serving.** In the API: compute the cell, fetch its pool (R2 is the
proposed store; about 100 GB+ for metros, no egress fees), filter to the
band, `rank_candidates`, add a connector from the user's exact point to the
pool's origin node (up to about 140 m), fall back to live generation on a
miss. Decide what "live" means at US scale: Vercel cannot hold US graphs, so
either a small routing server that loads tile graphs from R2, or answer
"not covered yet" outside precomputed areas.

### Step 4

**H. US run.** Estimate before measuring: 3.35M cells (top 50
metros at 200 m, from `config/metros.json`) x 27 s = 25,103 CPU-hours,
about 63 h on 400 cores, if D is done. Check
`scontrol show config | grep -i MaxArraySize` (often 1001) and split into
several arrays. Write shards to `/bigtemp`.

## Open decisions

- Targets: whole miles (current) vs half miles (about 2x compute, better
  coverage of half-mile requests).
- Grid: 200 m (current) vs 300 m (2.25x fewer cells, start up to 210 m away).
- Tile sizing: `--max-miles` 12 vs 15 and `--core-meters` (see A).
- Live fallback architecture (see G).
- Pool hosting (R2 proposed).
- OSM data is ODbL and pools are a derived database: attribution, and
  share-alike if pools are published.
- Rebuild cadence (OSM changes; proposal: quarterly, versioned by commit and
  graph hash).

## Checks before merging anything that touches routing

```bash
.venv/bin/python -m pytest -q
RUNLINE_ALLOW_OSM_DOWNLOAD=0 .venv/bin/python scripts/bench.py --repeat 1 --output /tmp/bench.json
.venv/bin/python scripts/shape_audit.py
```

Compare against **Measured**. Routes changing is fine when intended; say so
in the PR with the before/after numbers.
