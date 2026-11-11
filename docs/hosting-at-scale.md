# Hosting the metros at scale

Where do the top 50 metros live once the cluster batches finish? This page
projects disk and memory from the Washington, DC pilot, compares four ways to
host them, and lists what to measure when `b1` (New York and Los Angeles)
lands. Every metro number here is a projection, not a measurement.

Short answer: `b1` and `b6` fit the existing Oracle VM's disk as is. All 50
do not (about 241 GB of tile graphs and pools against 190 GB), and the
current tile graph format, not disk, is the real limit: a 24 GB box holds
about 33 DC-weight tiles in memory, and perhaps 16 of New York's. Add a block
volume when the disk runs out, and plan a compact tile graph format as the
fix that matters.

## Inputs

Measured on the Oracle VM (`VM.Standard.A1.Flex`, 4 OCPUs, 24 GB RAM,
190 GB boot volume), from [handoff.md](handoff.md) task 4:

| DC pilot | Value |
|---|---:|
| Tile graphs | 230 files, 4.2 GB (18.3 MB a tile) |
| Nodes per tile | 49k-124k |
| Load one tile | 2-4 s, about 650 MB resident once serving has used it |
| `RUNLINE_TILE_GRAPHS_CACHE=16` | about 10.5 GB |
| `pools.sqlite` | 2.3 GB: 72,237 pooled of 93,183 planned cells (77.5%) |
| First request in a tile / later | 6-9 s / 0.2-0.9 s |

Charlottesville's rebuilt complete pool (95 MiB, 11,417 pools, 605,206
loops) works out to about 26 KB per pooled cell, a little under DC's 31.8 KB,
so DC's pool density is a reasonable upper reference.

The metro configs: `config/metros.json` lists 50 metros with 8,677 tiles and
3,350,125 planned cells (3,347,052 after removing cells two metros share).
`config/metro-batches.json` records each batch's share of the all-metro
plan. Checked with `runline.tiling.owned_cells`: 8,675 tiles own at least one
cell, and the batch cell counts in the config are exact. (The `8,447` tile
total in `slurm/README.md` predates the current config.)

## How the projection works

`scripts/project_hosting.py` reads both configs and scales the DC figures:

```bash
PYTHONPATH=src .venv/bin/python scripts/project_hosting.py \
    --group dc-baltimore=washington-dc,baltimore-md --json hosting.json
```

- **Tile graphs scale with tile count**, at DC's 18.3 MB a tile, times a
  tile weight factor: low 0.6 (a Charlottesville-density tile: its 12 km
  graph is 7.9 MB, about 10.4 MB at a tile's 13.8 km), mid 1.0, high 1.5.
- **Dense metros get a high of 2.0.** A metro is dense when its population
  per planned cell is at least 1.25 times DC's: New York (1.36), Los Angeles
  (1.92), San Francisco (1.58), San Jose (1.61) and Las Vegas (1.31). Denser
  grids and fully mapped sidewalks mean more nodes per tile. Population density
  is only a proxy for street density, so this range is a guess, and New York's
  core tiles in particular could be heavier than 2x DC's average while its
  suburban tiles are lighter.
- **Pools scale with planned cells**: 24.7 KB per planned cell (DC's pooled
  share) for low and mid, 31.8 KB per cell for high (every planned cell
  pooled at DC's density).
- **RAM is per worker**, since each uvicorn worker keeps its own tile cache:
  1 GB for the OS and Caddy, plus per worker 1.5 GB of overhead (baseline,
  live-planning crops, memos; estimated from the Charlottesville
  measurements in `deploy/README.md`) and `cache` tiles at 650 MB times the
  tile factor. A batch takes its heaviest metro's factor, because the hot
  tiles are the dense cores.
- After a batch is built, `--graph-mb-per-tile` and `--tile-ram-mb` replace
  DC's figures with measured ones.

Decimal GB throughout, as the pilot was reported.

## Projections

Cache 16, one worker. Mid (low-high).

| Set | Metros | Tiles | Cells | Tile graphs GB | Pools GB | Disk GB | RAM GB | Hot tiles in 24 GB |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| `b1`: New York, Los Angeles | 2 | 907 | 372,064 | 16.6 (9.9-33.1) | 9.2 (9.2-11.8) | 25.7 (19.1-45.0) | 12.9 (8.7-23.3) | 33 (16-55) |
| New York alone | 1 | 634 | 257,373 | 11.6 (6.9-23.2) | 6.4 (6.4-8.2) | 17.9 (13.3-31.3) | 12.9 (8.7-23.3) | 33 (16-55) |
| `b6`, all 10 metros | 10 | 1,324 | 493,968 | 24.2 (14.5-36.8) | 12.2 (12.2-15.7) | 36.4 (26.7-52.5) | 12.9 (8.7-23.3) | 33 (16-55) |
| DC + Baltimore (part of `b6`) | 2 | 355 | 142,964 | 6.5 (3.9-9.7) | 3.5 (3.5-4.6) | 10.0 (7.4-14.3) | 12.9 (8.7-18.1) | 33 (22-55) |
| All 50 | 50 | 8,677 | 3,347,052 | 158.4 (95.1-248.4) | 82.6 (82.6-106.6) | 241.1 (177.7-355.0) | 12.9 (8.7-23.3) | 33 (16-55) |

`b6` is not just DC and Baltimore: the batch config puts ten metros in it
(Denver, Riverside, Baltimore, St. Louis, Portland, San Antonio, Sacramento,
Orlando, San Jose and Washington). Both rows are shown. San Jose is the dense
one that sets `b6`'s high RAM.

What the disk column leaves out: shards and extracts (the cluster archive,
not needed for serving), `plan.json.gz` (a few MB), the old DC pilot (6.5 GB)
while it is still served, and one batch's `pools.sqlite` of free space for a
`merge_pools.py --add-index` rewrite (up to about 16 GB for `b6`). The
runbook's 260-540 GB archive estimate is these serving files plus the shards.

### What fits the current VM

**Disk.** Allowing 15 GB for Ubuntu, the checkouts, the venv and caches,
about 175 GB is usable. `b1` + `b6` + the old pilot + rewrite room is about
85 GB mid and 120 GB high: it fits. All 50 at 241 GB mid does not; only the
low case (178 GB) comes close, with no headroom.

**RAM.** At 650 MB a tile, one worker with 16 cached tiles needs about
12.9 GB, well inside 24 GB, and could hold about 33 DC-weight tiles. If New
York's tiles are twice DC's, 16 tiles is 20.8 GB of graphs and about 23.3 GB
in all: too close to 24 GB to run live planning safely. Two workers halve
the hot tiles per worker (15 at mid, 7 heavy) because each holds its own
copy of the same tiles. Until New York is measured, keep one worker and set
the cache to about 12 for `b1` (18 GB at the heavy case).

RAM does not grow with the number of metros served, only with how many
distinct tiles are hot at once. It is a latency question: every request
outside the hot set pays a cold tile load.

### Cold tile latency

A request on a tile not in memory measured 6-9 s on the VM, against
0.2-0.9 s warm. The parts:

| Step | Time | Source |
|---|---|---|
| Read 18 MB from disk | under 0.1 s from the page cache; 0.04-0.3 s cold | volume throughput below |
| sha256 graph check (once per file per process) | about 0.1 s | not split out on the VM; `measure_tile_graphs.py` reports it |
| Unpickle into networkx | 2-4 s | measured |
| First-request work (node index, memos, pool reads) | the rest, about 2-5 s | inferred from the 6-9 s total |
| Fetch from object storage, if used | about 0.2-1 s for 18-37 MB | estimate; not measured |

Two side effects matter more than the seconds. The unpickle holds Python's
GIL, so a cold load stalls every other request on that worker for 2-4 s.
And heavier New York tiles will load proportionally slower; at 2x DC that is
4-8 s of unpickle alone.

## Options

Prices are list prices found on November 11, 2026. Oracle's own price pages
refused automated fetches, so Oracle figures come from the sources listed at
the end; check them in the OCI cost estimator before committing.

### (a) Add an Oracle block volume to the existing VM

Attach one volume (or grow the boot volume, same price) and point a second
`RUNLINE_METROS_DIR` run at it. No code change.

- Always Free block storage is 200 GB total, boot volumes included, so the
  190 GB boot volume has used almost all of it; an added volume is billed.
- Block storage is $0.0255 per GB-month; the Balanced level adds 10 VPUs at
  $0.0017 per VPU, $0.0425 per GB-month in all.

| Volume | Lower Cost (0 VPU) | Balanced (10 VPU) | Lower Cost / Balanced performance |
|---|---:|---:|---|
| 250 GB (all 50, mid, beside the boot disk's batches) | $6.38/mo | $10.63/mo | 500 IOPS, 60 MB/s / 15,000 IOPS, 120 MB/s |
| 400 GB (all 50, high case, with room) | $10.20/mo | $17.00/mo | 800 IOPS, 96 MB/s / 24,000 IOPS, 192 MB/s |

Lower Cost reads an 18 MB tile in about 0.3 s, small next to the unpickle,
and a pool request reads a few SQLite rows. It is enough for this traffic,
but its IOPS have no SLA. This solves disk only; RAM and cold latency stay as
they are.

### (b) Object storage with on-demand fetch into a local LRU

Keep the full set of tile graphs (and pools) in a bucket and fetch a tile
the first time a request needs it, into a local disk cache that evicts the
least recently used tile (the `tile-fetch` branch is prototyping this).

Storage for the full 241 GB (355 GB high):

| Store | Price | All 50, mid / high | Egress to the VM |
|---|---|---:|---|
| OCI Object Storage, Standard | about $0.0255/GB-month, 10-20 GB free | $5.89 / $8.80 per month | 10 TB/month outbound free |
| Cloudflare R2 | $0.015/GB-month, 10 GB free | $3.47 / $5.18 per month | free |
| Backblaze B2 | $6.95/TB-month, 10 GB free | $1.61 / $2.40 per month | free up to 3x stored per month |

Notes for the design:

- **Pools need a per-tile unit.** A batch's `pools.sqlite` is one 9-16 GB
  file, so it cannot be fetched per request. Either keep pools whole on local
  disk (83-107 GB for all 50, which leaves about 70-90 GB of the boot volume
  for a tile LRU, 3,700-5,000 DC-weight tiles), or split them into per-tile
  SQLite files (about 9.5 MB a tile) and fetch a tile's graph and pools
  together.
- **The hash check already protects downloads.** Serving hashes each tile
  graph against the merged file's `tiles` table, so a truncated or wrong
  object fails closed. A fetched file gets a new mtime, which resets the
  per-mtime caches; that is the right behavior.
- **Latency cost is small**: about 0.2-1 s on top of a 6-9 s cold tile, once
  per tile until the disk LRU evicts it.
- **It doubles as the second copy.** The runbook wants a copy of the serving
  files off UVA; a bucket is that copy whether or not the VM fetches from it.

This solves disk at the lowest storage price and keeps the VM replaceable,
but it adds code and a cold-fetch path to operate. It does nothing for RAM.

### (c) Shrink the tile graph format

A tile graph is a pickled `networkx.MultiDiGraph` (`build_tile_graphs.py`
writes `gzip(pickle(graph, protocol=5))`; `metros.read_graph_file` unpickles
it). Every node and edge is a Python dict, and edges with bends carry a
shapely `LineString`. Serving only needs node coordinates, elevation and
signal tags, and per edge its length, highway class, surface, name/ref for
major roads and its geometry. Unused at serve time: `osmid` on edges,
`oneway`, `reversed`, `service`, `maxspeed`, `lanes`, `bridge`, `tunnel`,
`access`, `width` and `street_count`.

Measured locally on the Charlottesville graph (40,209 nodes, 105,020 edges,
413,452 geometry points), each variant loaded in a fresh process:

| Variant | File | Load | Resident |
|---|---:|---:|---:|
| Current pickle | 7.9 MB | 0.31 s | 174 MB |
| Unused attributes dropped | 7.4 MB | 0.27 s | 163 MB |
| ... and geometry as float32 arrays | 5.1 MB | 0.23 s | 173 MB |
| Flat arrays: CSR adjacency, float32 coordinates and lengths, packed geometry | 2.6 MB gzipped (6.5 MB raw) | 0.01 s | 11 MB |

Trimming attributes is not worth a rebuild (6%). The win is leaving networkx
for arrays: about 3x smaller on disk and about 16x smaller in memory. For a
DC tile that suggests 650 MB down to roughly 40-80 MB once used (the memos
would also need to be arrays), and loads in tens of milliseconds instead of
seconds.

Projected with `--graph-mb-per-tile 6.1 --tile-ram-mb 80 --cache 100`:

| | Tile graphs GB | Disk GB | Hot tiles per worker in 24 GB |
|---|---:|---:|---:|
| All 50, gzipped arrays | 52.9 (31.8-83.0) | 135.5 (114.4-189.5) | 268 (134-447) |
| All 50, raw arrays for mmap | 131.9 (79.1-206.8) | 214.5 (161.7-313.3) | 268 (134-447) |

Gzipped arrays put all 50 metros on the existing 190 GB disk at the mid
case. Raw `.npy` files opened with `mmap` would instead let every worker
share one copy through the page cache, which matters if the VM runs more
than one worker, but they need about as much disk as today's pickles.

The cost is code, not money:

- `serving.py` runs `nx.single_source_dijkstra` (both directions, 300 m
  cutoff), `_route_graph` and `measure_route` on the networkx graph; these
  would read the arrays, or build the small route graph from them.
  `osm._CostMatrix` already runs scipy's Dijkstra on a sparse matrix.
- Live planning (`crop_to_radius`, `collapse_graph`, `generate_loops`) could
  stay on networkx by building the cropped subgraph from the arrays per
  request.
- The new file changes every tile's hash. Pools store OSM node ids, which the
  arrays keep, so pools stay valid, but the merged file's `tiles.graph`
  hashes would need rewriting by a converter that proves the conversion kept
  every node and edge.
- `requirements.txt` pins networkx because pickles only load under the
  versions that built them; plain arrays remove that coupling.

Pools are already compact (format 2: delta varints, lzma), so the arrays do
not touch them. After (c), pools are the larger half of the disk.

### (d) Per-region VMs

Split metros across two or more VMs (say east and west), each serving its
own runs, with the combined API routing a start to its region.

- Always Free compute is home-region only and, since June 15, 2026, the
  documented Always Free A1 allowance is 1,500 OCPU-hours and 9,000 GB-hours
  a month (2 OCPUs and 12 GB), down from 4 and 24. A second VM is billed in
  full.
- Oracle's Arm page lists A1 at $0.01 per OCPU-hour and $0.0015 per GB-hour;
  another Oracle table shows $0.013106 and $0.0019659. A second 4 OCPU, 24 GB
  VM is $55.48-72.71 a month, a 2 OCPU, 12 GB one $27.74-36.36, plus its
  disk.
- Routing: `RUNLINE_REMOTE_API` is a single URL today, so the combined API
  would need a start-to-region map.

It doubles RAM for hot tiles and halves each box's disk, but a cross-country
round trip (well under 0.1 s) is nothing next to a cold tile, so it buys no
latency. Every number above says one box is enough for this traffic once
disk (a, b) or format (c) is fixed. Not recommended.

## Recommendation

1. **Now, for `b1` and `b6`: change nothing but the cache.** Both batches,
   the old pilot and rewrite room fit the existing boot volume (about 85 GB
   mid, 120 GB high). Run one worker and lower `RUNLINE_TILE_GRAPHS_CACHE` to
   about 12 while serving `b1`, until New York's tiles are measured.
2. **Put every finished batch's serving files in a bucket** (B2 or R2,
   $2-5 a month for all 50) as the off-UVA copy the runbook asks for. This
   is worth doing whether or not the VM ever fetches from it.
3. **When the disk runs out, add a block volume.** With the old pilot and
   rewrite room on disk, that comes after about four batches in the high
   case and six at mid. Add 250 GB Lower Cost at about $6.40 a month, or
   400 GB if `b1` measures heavy. It is the only option that needs no code. Prefer it over
   on-demand fetch unless `tile-fetch` is already merged and tested; at this
   traffic a disk LRU saves a few dollars a month in exchange for a new
   failure path.
4. **Plan the compact tile format (c) as the real fix.** It is the only
   option that changes RAM and cold latency, and with gzipped arrays all 50
   metros fit the existing disk at the mid case (136 GB) without (a) or (b). Do it after `b1` is
   measured, and before the full 50 go live.
5. **Skip per-region VMs.**

## What to measure first when `b1` lands

1. `build_tile_graphs.py summary --output <b1 tile-graphs>`: mean and
   largest file size and node count for New York and Los Angeles. Rerun
   `project_hosting.py --graph-mb-per-tile <mean>`; this replaces the
   0.6-2.0 factor guess.
2. On the VM (Linux, ARM, the serving versions):
   `scripts/measure_tile_graphs.py <b1 tile-graphs> --largest 8 --together 4`.
   It reports load time, resident MB, hash and index time per tile and
   whether memory adds up across tiles. Pass the per-tile resident MB as
   `--tile-ram-mb` and set `RUNLINE_TILE_GRAPHS_CACHE` from the hot-tile
   column.
3. `b1`'s `pools.sqlite` size against its 372,064 cells: KB per planned cell
   and the pooled share, against DC's 24.7 KB and 77.5%.
4. First-request and warm latency on the largest Manhattan-area tile after a
   service restart, and the peak RSS during a 15 mile live request on it.
5. `measure_tile_graphs.py` on one converted array tile, if (c) is
   prototyped, to confirm the 16x memory figure holds on a real tile.

## Open questions

- How many uvicorn workers does the Oracle unit run? The handoff records the
  cache size but not the worker count; each worker multiplies the tile RAM.
- Billing: the VM is 4 OCPUs and 24 GB on a Pay As You Go account. The
  documented Always Free A1 allowance halved on June 15, 2026. Reports differ
  on whether Pay As You Go tenancies are now charged for the second half
  (about $28-36 a month). Check the account's cost analysis.
- Which metros does the public app need first? Serving a subset (for
  example the first four batches) may never need (a) or (b).

## Sources

- Measured DC pilot facts: [handoff.md](handoff.md), task 4.
- Charlottesville worker memory: [deploy/README.md](../deploy/README.md).
- Oracle Always Free limits (200 GB block storage including boot volumes,
  1,500 OCPU-hours and 9,000 GB-hours of A1, 20 GB object storage, 10 TB
  outbound): https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm
- The June 15, 2026 A1 allowance change and the Pay As You Go uncertainty:
  https://infoq.com/news/2026/07/oracle-cloud-free-tier-limits/ and
  https://linuxiac.com/oracle-quietly-cuts-free-tier-ampere-a1-resources-in-half/
- Block volume performance levels (IOPS and KB/s per GB):
  https://docs.oracle.com/en-us/iaas/Content/Block/Concepts/blockvolumeperformance.htm
- Block volume and object storage prices ($0.0255 per GB-month storage,
  $0.0017 per VPU per GB-month): https://www.oracle.com/cloud/price-list/
  (blocked automated fetches; figures from its search listing and
  https://oraclelicensingexperts.com/blog/oracle-oci-storage-pricing/)
- A1 compute prices: https://www.oracle.com/cloud/compute/arm/pricing/ and
  https://www.oracle.com/cloud/iaas-paas/ (via search listings)
- Cloudflare R2: https://developers.cloudflare.com/r2/pricing/
- Backblaze B2: https://www.backblaze.com/cloud-storage/pricing
