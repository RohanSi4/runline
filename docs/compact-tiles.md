# Compact tile graphs

Metro tile graphs are pickled `networkx.MultiDiGraph`s with shapely
geometries. On the Oracle VM a DC tile holds about 650 MB once serving has
used it and takes 2 to 4 s to unpickle; all 50 metros project to about
215 GB on a 190 GB disk, and 24 GB of RAM holds about 33 hot tiles
([hosting-at-scale.md](https://github.com/RohanSi4/runline/blob/hosting-sizing/docs/hosting-at-scale.md),
option (c)). This branch makes the compact format real:

- **Phase 1:** a compact file (`<name>.npz`) beside each pickle, written by
  `scripts/compact_graphs.py --verify`, that loads back into the same
  networkx graph. It is 2.7x smaller on disk and loads about 2x faster, and
  pools built on the pickle keep validating against it.
- **Phase 2:** pool hits on a compact metro tile are answered from its
  arrays (`runline.array_tiles`) without building the networkx graph, so a
  hot tile holds about 13 MB instead of about 113 MB (Charlottesville-sized
  tiles, eight held at once). The graph is built from the arrays only when a
  request there plans live.

Every route, distance and metric comes out identical: the test suite,
`scripts/bench.py` and `scripts/shape_audit.py` match exactly between pickle
and compact loading (see [Equivalence](#equivalence)).

## Format (version 1)

One `.npz` (a zip of `.npy` arrays, deflate compressed by
`numpy.savez_compressed`), read with `allow_pickle=False`, so loading one
runs no code. `runline.compact` is the reference reader and writer.

`header` is a `uint8` array holding UTF-8 JSON:

| Field | Meaning |
|---|---|
| `format`, `version` | `"runline-compact-graph"`, `1`. Any other version is refused. |
| `nodes`, `edges` | Counts. |
| `graph` | The graph attributes (`crs`, `elevation_baked`, `simplified`, `created_*`). Per-process memos (`_runline_*`) are never stored. |
| `node_tags`, `edge_tags`, `node_floats`, `edge_floats` | The kept attributes, in the order the loader sets them. |
| `encodings` | Per coordinate array: `delta-fixed` or `float64` (below). |
| `vocabularies` | Per tag attribute, the distinct values as JSON. Index 0 means the attribute is absent. |
| `source_fingerprint` | The 16-hex sha256 prefix of the pickle this was made from: the hash pools record. |
| `source_name` | That pickle's file name, for people. |
| `content_sha256` | sha256 over the format name, this header (less this field, canonical JSON) and every array's name, dtype, shape and bytes, in name order. |

Arrays (N nodes, E edges, P distinct successor pairs, G edges with a
geometry, L distinct lines, Q line points):

| Array | dtype, shape | Content |
|---|---|---|
| `node_ids` | int64 [N] | OSM node ids in the graph's node order |
| `node_x`, `node_y` | int32 or float64 [N] | Longitude and latitude |
| `node_elevation`, `node_elevation_present` | float64, uint8 [N] | Elevation in metres and whether the node has one |
| `node_tag_<name>` | uint8/16/32 [N] | Index into `vocabularies["node.<name>"]` |
| `succ_offsets` | int64 [N+1] | CSR: node i's edges are rows `succ_offsets[i]` to `succ_offsets[i+1]` |
| `edge_target` | int32 [E] | Target node position, rows in successor order then key order |
| `edge_key` | uint8 or int64 [E] | The multigraph key |
| `pred_offsets`, `pred_sources` | int64 [N+1], int32 [P] | Each node's predecessors in the graph's own predecessor order |
| `edge_length`, `edge_length_present` | float64, uint8 [E] | `length` in metres, exactly as stored |
| `edge_tag_<name>` | uint8/16/32 [E] | Index into `vocabularies["edge.<name>"]` |
| `edge_has_geometry` | uint8 [E] | Whether the edge has a `geometry` |
| `geometry_line`, `geometry_reversed` | int32, uint8 [G] | For each edge with a geometry, its line and whether it runs backwards |
| `line_offsets` | int64 [L+1] | Line k's points are `line_offsets[k]` to `line_offsets[k+1]` |
| `geometry_x`, `geometry_y` | int32 or float64 [Q] | Line points |

Encodings:

- **Coordinates.** OSM coordinates have seven decimals, so the writer stores
  `round(value * 1e7)` as int32 deltas (`delta-fixed`) when every value
  divides back to the identical float64; otherwise it stores float64. The
  check is per array at write time, so this is lossless by construction.
- **Lines.** A two-way street's two edges carry the same line, one reversed;
  each distinct line is stored once. On Charlottesville exactly half of the
  55,566 geometries are reversed copies.
- **Lengths and elevation** stay float64. Lengths are not reproducible from
  the coordinates bit for bit, and planning sums them.
- **Tags** keep their Python type through JSON: a string stays a string and
  a list (OSM's multi-valued ways) stays a list, one fresh list per edge.

Order is part of the format. The writer lists nodes in the graph's node
order, each node's successors in insertion order, each successor's keys in
insertion order, and each node's predecessors in their own order. The
loader fills networkx's `_node`, `_succ` and `_pred` dicts directly in that
order, sharing one key dict between the successor and predecessor sides as
`add_edge` does. Node order decides `_nearest_node` ties and the scipy cost
matrix's indices; neighbour order decides Dijkstra ties; predecessor order
decides serving's reverse connector search. Rebuilding the graph with
`add_edge` in successor order would put every node's predecessors in
successor-derived order. On both Charlottesville graphs that happens to be
their order already, but nothing guarantees it for a graph built another
way (a test graph in `tests/test_compact.py` differs), so it is stored.

## Attribute inventory

What the code reads from a walk graph (`grep` over `src/` and `scripts/`):

| Attribute | Read by |
|---|---|
| node `x`, `y` | everywhere: `_node_index`, `crop_to_radius`, `_edge_geometry_coordinates`, `_compactness`, `_major_crossings`, elevation, starts, point-to-point snapping |
| node `elevation` | `measure_route` (climb, elevation coverage) |
| node `highway`, `crossing`, `crossing:signals` | `osm.is_signal` (signal waits, edge costs); `rebuild_area.py` |
| edge `length` | edge costs, `measure_route`, `_major_crossings`, `_edge_lengths`, serving's connector search, bench |
| edge `highway` | `_is_major`, `_is_trail` (costs, metrics, crossings); `offline_demo.py` |
| edge `surface` | `_is_trail` |
| edge `name`, `ref` | `_major_crossings` (one crossing per named road) |
| edge `geometry` | `_edge_geometry_coordinates` (drawn routes, bearings, doubling back), `audit_crossings.py`, `offline_demo.py` |
| edge `bridge`, `tunnel` | `audit_crossings.py` geometric crossings, which `bench.py` reports |
| graph `elevation_baked` | planner (skip Open-Meteo), precompute warning |
| graph `coverage_limited` | set at request time on a crop, never stored |

Dropped: node `street_count`, `ref`, `junction`; edge `osmid`, `oneway`,
`reversed`, `service`, `maxspeed`, `lanes`, `access`, `width`, `junction`.
Only `tests/test_pbf.py` reads `osmid`, on freshly built graphs. `surface`
is absent from every graph built today (osmnx's default way tags omit it) but
the code reads it, so it is kept. Adding an attribute later means adding it
to `NODE_TAGS` or `EDGE_TAGS` and reconverting.

Two type changes are deliberate: `length` comes back as a Python `float`
instead of `numpy.float64` (same value, same arithmetic), and geometry
LineStrings are new objects with identical coordinates.

## Hash compatibility

Pools record the graph they were built on as `sha256(file)[:16]` (the area
pool's `meta.graph`, a metro file's `tiles.graph`). The DC, New York and Los
Angeles pools on the VM were built against pickles and must keep working
without recomputation. The design:

1. **The compact file carries its pickle's hash.** `convert` hashes the
   pickle's bytes and records them as `source_fingerprint`.
2. **It also carries its own content hash.** `content_sha256` covers the
   header and every array, so a flipped bit, a truncated copy or an edited
   array is caught before anything is served. A broken zip raises before
   that (zip CRCs).
3. **One fingerprint function for both kinds.**
   `runline.compact.graph_fingerprint(path)` (which `serving._fingerprint`
   and `precompute_pools.py` call) returns a pickle's byte hash, or a
   compact file's recorded `source_fingerprint` once its content hash
   verifies; a damaged compact file gets `invalid-compact`, which no pool
   matches. A pool built on the pickle therefore validates against the
   compact file, a pool built on a different graph is still refused, and a
   tampered compact file is refused.
4. **One choice of file for loading and checking.**
   `runline.compact.graph_file("<name>.pkl.gz")` picks what to serve:

   | On disk | Served |
   |---|---|
   | pickle only | the pickle |
   | compact only | the compact file (if damaged: pools refused, loading reports "Map data ... is damaged", a 422) |
   | both, compact intact and made from this pickle | the compact file |
   | both, compact damaged, or made from another pickle | the pickle, with a warning |

   The "both" check hashes the pickle once per file version per process
   (about 0.1 s for an 18 MB tile), so a stale compact file left beside a
   rebuilt pickle cannot shadow it. Serving's per-file caches key on the
   chosen file and its mtime.
5. **Loading verifies too.** `read_graph` checks the content hash before
   building anything, so a file damaged after the pool check still fails
   closed.

The trust model is unchanged: a compact file is as trusted as the pickle it
replaces. The content hash detects damage, not an attacker who can rewrite
files (who could replace a pickle with code). Compact files do remove the
code-execution risk of unpickling.

Tests (`tests/test_compact.py`, `tests/test_metros.py`):

- a pool built on the pickle validates against its compact file, for an
  area pool and for a metro run whose tile graphs are converted in place,
  with and without the pickles left beside them;
- a tampered compact file (one length changed, recorded hash kept), a
  flipped byte and a truncated file are refused: `invalid-compact`, pools
  ignored, a 422 that says the map data is damaged;
- a compact file of another tile copied over this tile's is refused
  ("built from graph ..."), and so is a pool from another graph;
- a compact file made from an older pickle, or a damaged one, does not
  shadow the pickle beside it.

## Measurements

MacBook (arm64, Python 3.13, the pinned versions), each graph loaded in a
fresh process by `scripts/measure_tile_graphs.py` (`--compact` for `.npz`).
"Graph MB" is resident memory added by the load. The 4 km and 8 km rows are
crops of the rebuilt Charlottesville graph; the full graphs are 12 km discs,
close to a metro tile's 13.8 km.

| Graph | Nodes | Pickle MB | Compact MB | Hash s (pickle / compact) | Load s (pickle / compact) | Graph MB (pickle / compact) | Arrays MB (pool hits) |
|---|---:|---:|---:|---:|---:|---:|---:|
| Charlottesville, repository | 40,209 | 7.5 | 2.8 | 0.00 / 0.01 | 0.23 / 0.12 | 158 / 144 | 20 |
| Charlottesville, rebuilt | 40,422 | 7.5 | 2.8 | 0.00 / 0.01 | 0.23 / 0.13 | 156 / 145 | 20 |
| 8 km crop | 34,750 | 5.9 | 2.3 | 0.00 / 0.01 | 0.16 / 0.10 | 135 / 119 | 17 |
| 4 km crop | 21,898 | 3.6 | 1.4 | 0.00 / 0.01 | 0.11 / 0.06 | 87 / 73 | 10 |

Sizes are MiB, as `measure_tile_graphs.py` prints them: the full graphs
are the original 7.9 MB (decimal) files, which become 2.9 MB. The crops
were pickled at gzip level 6, as `build_tile_graphs.py` writes tiles.
Repeated runs moved the memory columns by a few MB and load times by
0.01 s.

The compact hash column is verification: decompressing and hashing the
arrays. "Arrays MB" is a fresh process holding one `ArrayTile` after a
connector search (12.2 MB of arrays for the full graph; the rest is
allocator overhead that does not repeat per tile). Eight tiles held in one
process:

| Held as | Resident over baseline | Per tile | Load all eight |
|---|---:|---:|---:|
| Pickled networkx graphs | 904 MB | 113 MB | 3.2 s |
| Compact files rebuilt as networkx graphs | 731 MB | 91 MB | 1.0 s |
| `ArrayTile` (pool hits) | 104 MB | 13 MB | 0.14 s |

Other measurements:

- Live planning on the compact graph is cheaper too: cropping and
  collapsing a 5 mile request took 0.65 s instead of 0.83 s and added 47 MB
  instead of 83 MB, since every edge copies fewer attributes.
- Converting a Charlottesville-sized graph takes about 1.2 s, and
  `--verify` adds about 1.1 s (reload and compare every node and edge).
- `bench.py`, one cold pass over 32 cases (indicative; the machine was
  busy): median end to end 1.53 s pickle, 1.38 s compact on the repository
  graph; peak RSS 656 MB and 592 MB.

What this projects for the VM (to be confirmed with
`measure_tile_graphs.py <tile-graphs> --compact` there): resident memory
scales with nodes, about 3.9 MB per 1,000 nodes as pickles and 0.33 MB per
1,000 nodes as arrays, so a 124k-node DC tile would hold roughly 40 MB as
arrays against the 650 MB measured. Disk shrinks 2.7x: `b1`'s 13.8 GB of
tile graphs would be about 5 GB, and all 50 metros' tile graphs (about
132 GB at `b1`'s 15.2 MB a tile) about 49 GB, which with pools puts the 50
metros near 132 GB, inside the 190 GB disk.

## Equivalence

Pickle versus compact loading, same code:

- **Tests.** `tests/test_compact.py` round-trips the shipped graph with
  `compare_graphs` (node order, successor and predecessor order, keys and
  every kept attribute), and plans 3, 4 and 5 mile loops on three surfaces
  plus a point-to-point route on both, comparing every candidate field.
  `tests/test_metros.py` serves every pooled request of the toy metro run
  from pickles, from compact tiles through networkx, and from compact tiles
  through arrays, and compares them; it also checks a live request on a
  compact tile. The whole suite passes (251 tests) along with
  `node --test tests/web_flow.mjs tests/run_session.mjs` (35).
- **`scripts/bench.py`** (`--repeat 1`, the 8 public Charlottesville origins
  times 3, 5, 8 and 12 miles) with `RUNLINE_GRAPHS_DIR` pointing at a
  directory holding only the pickle, then only the compact file. Every
  field except timings is identical, on both graphs:

  | Graph | Cases | Routes | Feasible top 3 | Signals | Crossings (proxy / geometric) | Mean distance error |
  |---|---:|---:|---:|---:|---:|---:|
  | Repository, pickle and compact | 32 | 96 | 91 | 38 | 265 / 257 | 0.127 mi |
  | Rebuilt, pickle and compact | 32 | 96 | 93 | 74 | 272 / 265 | 0.124 mi |

- **`scripts/shape_audit.py`** output is byte-identical between pickle and
  compact on both graphs:

  | Graph | Routes | Compactness < 0.35 / < 0.2 | Median compactness | Dead ends | Short revisits | Repeated > 10% |
  |---|---:|---:|---:|---:|---:|---:|
  | Repository | 96 | 37 / 9 | 0.389 | 0 | 0 | 13 |
  | Rebuilt | 96 | 37 / 7 | 0.389 | 0 | 3 | 12 |

`osm.py`, `scoring.py` and `models.py` are unchanged on this branch.

## Phase 2: serving without the networkx graph

What a pool hit reads from the tile graph (`serving.serve_from_pool`),
profiled on the rebuilt Charlottesville graph with pools built locally by
`precompute_pools.cell_pool` for the nine cells around the map centre
(3, 5 and 8 mile targets, 270 loops):

| Step | Reads | 5 mi hit, graph | 5 mi hit, arrays |
|---|---|---:|---:|
| `_nearest_node` | all node coordinates (an index built once per graph) | 3.2 ms first, then 0.1 | 0.1 ms |
| `_reach` | edges within 300 m each way of the start (428 nodes), `length` only | 0.8 ms | 0.8 ms |
| `_candidates` | pool data only | 1.3 ms | 1.3 ms |
| `_route_graph` | the 56 candidates' edges plus the major-road edges at their nodes, all kept attributes: a 2,022-node, 2,889-edge small graph | 27.8 ms | 32.4 ms |
| measure and rank | the small graph only | 62 ms | 62 ms |
| draw | the small graph only | 1.9 ms | 1.9 ms |

A hit touches a few thousand of a tile's 100k+ edges. Live planning, by
contrast, crops a disc of the whole graph, collapses it and builds a cost
matrix over it, so it genuinely needs every node and edge near the start.

**Implemented here** (`runline.array_tiles.ArrayTile`):

- `ArrayTile` keeps the compact file's arrays plus search arrays: each
  node's distinct successors with the minimum parallel `length` (the weight
  networkx's Dijkstra uses on a multigraph), the same for predecessors in
  their order, sorted ids for id lookup, and a major-road flag per edge.
- `reach` is networkx's `_dijkstra_multisource` (heap of distance, counter,
  node; cutoff; strict improvement) over those arrays, so it returns the
  same distances, paths and finishing order. Checked on 400 random starts
  and in the tests on 300 more.
- `route_graph` rebuilds `serving._route_graph`'s small DiGraph: the same
  node and edge insertion order (the major-road pairs come from the same
  set built from the same tuples in the same order), the same cheapest
  parallel edge, the same attributes. Geometry is built only for the chosen
  edge.
- The nearest-node index is handed to `osm._nearest_node` as the memo it
  would build, in the graph's node order.
- `runline.metros` keeps two caches: `TILE_ARRAYS` (default 32,
  `RUNLINE_TILE_ARRAYS_CACHE`) for pool hits, and `TILE_GRAPHS` (default 2,
  `RUNLINE_TILE_GRAPHS_CACHE`) for live planning, which builds the graph
  from the held arrays without rereading the file.
  `RUNLINE_ARRAY_SERVING=0` sends hits back through the networkx graph.
  Shipped areas (Charlottesville) are unchanged: their live path dominates.

Costs: a hit's `_route_graph` is about 5 ms slower cold and 15 to 20 ms
slower warm than on a held networkx graph (the graph keeps per-node memos
across requests; the arrays rebuild each edge's attributes). Against that,
a cold tile is ready in about 0.02 s instead of seconds, and memory per hot
tile drops about 9x.

**Next steps, not implemented:**

1. **Crop live requests from the arrays.** `crop_to_radius` keeps a disc of
   the graph; building only that disc from the arrays would avoid the full
   graph on live fallbacks too. To stay identical it must reproduce
   `graph.subgraph(keep).copy()`'s node order, which for a disc under half
   the graph follows the iteration order of a Python `set` of the kept ids.
   That is reproducible but ties the code to networkx internals, so it
   needs the same equivalence tests as `ArrayTile`.
2. **Memoize edge data per tile** (a bounded dict of built attribute dicts)
   if the warm-hit difference matters; it trades back some memory.
3. **mmap-able raw arrays.** With more than one uvicorn worker, storing
   uncompressed `.npy` members and memory-mapping them would share one copy
   per tile through the page cache; it costs about 2.5x the compressed disk.
4. **Drop networkx from the planner's search entirely** (cost matrix and
   collapse from arrays). `osm._CostMatrix` already runs scipy's Dijkstra;
   the remaining networkx use is graph construction and the per-route
   lookups in `measure_route`.

## Rollout on the Oracle VM

Paths from [handoff.md](handoff.md) task 4. The service reads tile graphs
from the shared `~/runline/pools/tile-graphs` (symlinked from both runs).

1. **Deploy the code** (after the PR merges): `git -C ~/runline-main fetch
   origin main && git -C ~/runline-main checkout --detach origin/main`. No
   new dependencies; numpy is already pinned.
2. **Check disk.** Compact files add about 37% of the pickles' size while
   both exist (about 7 GB for the 18 GB shared folder).
3. **Convert in place, verified**, two workers to leave the service room
   (each holds a pickled and a rebuilt graph at once, an estimated 1.5 GB
   for a DC tile):

   ```bash
   cd ~/runline-main
   .venv/bin/python scripts/compact_graphs.py ~/runline/pools/tile-graphs --verify --workers 2
   ```

   It prints one line per tile and ends with `N graphs, 0 failed`. A tile
   that fails verification has no compact file and keeps serving its pickle.
   Until the restart in step 5 the service runs the old code and ignores
   the `.npz` files. Each file appears by an atomic rename, so a service on
   the new code can never read a half-written one.
   Rerunning skips tiles already converted from the same pickle.
4. **Measure** one large tile before changing caches:
   `.venv/bin/python scripts/measure_tile_graphs.py ~/runline/pools/tile-graphs --compact --largest 4`.
5. **Restart** (`sudo systemctl restart runline`). Optionally raise
   `RUNLINE_TILE_ARRAYS_CACHE` (for example 200, about 8 GB at an estimated
   40 MB a DC tile; set it from step 4) and lower `RUNLINE_TILE_GRAPHS_CACHE`
   to 4 or so, since graphs are now held only for live requests.
6. **Confirm**: DC, New York and Los Angeles requests that came from pools
   before still return `"source": "pool"`, the first request in a tile now
   takes well under a second, and the journal has no `built from graph`
   or `refusing compact graph` warnings. `scripts/pool_smoke.py` checks area
   pool files the same way.
7. **Reclaim disk later**, once it has served for a while: delete the
   `.pkl.gz` tile graphs (keep the `<slug>.json` stats). Keep an archive
   copy of the pickles off the VM, since they are what the pools' hashes
   name.

**Rollback:** delete the compact files (`rm ~/runline/pools/tile-graphs/*.npz`)
and restart; serving falls back to the pickles, with no pool changes. To
keep the files but stop array serving, set `RUNLINE_ARRAY_SERVING=0`. If the
pickles were already deleted, restore them from the archive first.

New batches: build, precompute and merge on pickles as today, then convert.
`precompute_pools.py` also accepts compact tile graphs and records the same
pickle hash, so converting before precompute works too.
