# Pool misses on the complete Charlottesville data

On the complete rebuilt-graph pools, the 600-request check in
[presentation-release.md](presentation-release.md) answered 548 requests
(91.3%) from pools; 52 planned live. This note explains those 52, ranks the
fixes, and measures the two serving-side retries this branch adds. With
them, **582 / 600 (97.0%)** are answered from pools. Every request that
hit before returns the identical routes, and live planning is unchanged.

## Data

- Graph: the rebuilt Charlottesville graph, hash prefix `66b524897627167e`,
  which matches the pool file's recorded `graph` fingerprint.
- Pools: the complete-area file (SHA-256 prefix `c18a02a0`), 11,417 pools,
  605,206 loops, format 2, 200 m cells, targets 1 to 12 miles, all three
  surfaces, built at `9b56ec5`.
- Both were read in place through symlinks under the ignored `output/`
  directory; neither was modified or copied into the repository.

## Reproducing 548 / 600

`scripts/compare_pools.py` with its defaults (seed 7, 40 walkable nodes
within 6 km of the area centre, 3, 5, 6.5, 8 and 12 miles) on all three
surfaces is 40 x 5 x 3 = 600 requests. On current `main` it gives exactly the
recorded result: 548 hits, 20 of them with two routes, median pooled time
64 ms, p95 134 ms.

| Surface | Hits before |
|---|---:|
| Mixed | 184 / 200 |
| Road | 181 / 200 |
| Trail | 183 / 200 |
| All | 548 / 600 (91.3%) |

## Why the 52 missed

`scripts/pool_misses.py --without-retries` walks serving's steps for each
miss and records the first one that leaves too few routes. Serving's own
log reasons were 21 "no loop within reach", 15 "no pool", 14 "few in band",
1 "few distinct" and 1 "few without doubling back". Splitting those further:

| Reason | Misses | Starts | What happened |
|---|---:|---:|---|
| Runner off the connected graph | 15 | 1 | The start's nearest node is on a four-node fragment that reaches nothing. Serving joined loops from that node. Live planning starts from the largest strongly connected component instead, 42 m away. |
| No pool rows in cell + 8 neighbours | 15 | 1 | No clean loop exists there. The cell and its neighbours have no rows on any surface. The 5x5 and 7x7 rings hold 1 and 3 pooled cells, and none of them yields a route. Live planning returns out-and-backs with a median of 38% repeated distance and compactness 0.04. |
| Too few in band once joined | 15 | 5 | In-band loops pass within 300 m, but at most one still lands within ±0.25 mi after the connector is added and the route is measured as drawn. |
| Join point too far | 5 | 2 | In-band loops exist, but none passes within 300 m each way of the runner. The nearest is 714 m and 986 m round trip. |
| Fewer than 2 distinct after the 70% overlap dedupe | 1 | 1 | Two loops in band, both the same route as far as the runner is concerned. |
| All but one over the repetition limit | 1 | 1 | Two in band, one above the 10% repeated-distance limit. |
| No in-band loop stored at all | 0 | | |
| Fingerprint or tile mismatch | 0 | | The graph hash matches; Charlottesville is an area file, not a metro. |

By surface the 52 split 16 mixed, 19 road, 17 trail; by distance 13 at 3 mi,
12 at 5, 9 at 6.5, 6 at 8 and 12 at 12. Neither dimension explains much.

The misses cluster by start, not by request. 30 of the 52 come from two
starts that miss on every surface and distance. All 52 come from 7 of the 40
starts. Positions relative to the map centre, in 200 m cells (rows north,
columns east):

| Start | Misses | Offset (rows, cols) | Distance, direction | Nodes within 300 m | Reason |
|---|---:|---|---|---:|---|
| 33 | 15 | +10, +2 | 2.1 km N | 4 | Off the connected graph |
| 37 | 15 | -13, +11 | 3.4 km SE | 44 | No clean loop nearby |
| 7 | 8 | +13, -25 | 5.7 km NW | 21 | Join too far, few in band |
| 14 | 6 | +8, -1 | 1.5 km N | 48 | Few in band |
| 36 | 5 | +16, +13 | 4.1 km NE | 5 | Join too far, few in band |
| 34 | 2 | +4, +20 | 4.0 km E | 15 | 12 mi only: few distinct, few in band |
| 29 | 1 | +25, +13 | 5.6 km NE | 7 | Few in band |

Apart from the two whole-start failures, misses sit mostly where streets
are sparse. The seven missing starts reach a median of 15 nodes within
300 m; the other 33 reach 78. Four of the remaining five (7, 29, 34, 36)
reach 5 to 21 nodes and lie 4 to 6 km out, between north-west and east.
Start 14 is near the centre at ordinary density. Distance from the centre
alone is a weak signal: only 3 of the 13 starts beyond 4 km miss.

## Fixes, ranked

The counts below are misses each change answers alone, from the same
script's counterfactual reruns (`fixes` in its JSON).

| Fix | Misses answered | Risk | Status |
|---|---:|---|---|
| Start a stranded runner from the connected graph, as live planning does | 15 | Low: same start node live would use; hits never run it | Done |
| On a miss, retry with the 5x5 ring and a 500 m connector | 19 | Low to moderate: longer connectors, still measured and filtered | Done |
| 500 m connector alone | 16 | As above | Subsumed |
| 5x5 ring alone, or 7x7 | 2, or 3 | Low | Subsumed |
| Accept one route instead of two | 11 alone, 0 after the two retries | A product change: one choice where live offers three | Not done |
| Join loops partway along an edge, not only at nodes | Some of start 36's 3 | Moderate: touches anchoring and drawing | Documented |
| Second origin per sparse cell, or half-mile targets | Start 36's 3, maybe | Needs a pool recompute | Documented only |
| Anything for start 37's area | 0 | No clean loop exists; live's answer is poor too | None needed |

Together the two retries answer 34 misses: all 15 off-graph cases, 14 of the
15 "few in band", 3 of the 5 "join too far", plus the "few distinct" and
repetition cases. That leaves 18.

## What changed in serving

`serve_from_pool` is unchanged for any request its usual read answers. Only
when that read cannot answer does it try, in order:

1. **Connected start.** If the nearest node is off the graph's largest
   strongly connected component, move to the nearest node on it and retry.
   This matches `osm._routable_core`, which live planning uses. The
   component is computed once per loaded graph (about 70 ms on
   Charlottesville) on the first miss that needs it.
2. **Wider retry.** Add the 16 cells around the 3x3 (`FALLBACK_RING = 2`).
   Join loops up to `FALLBACK_CONNECTOR_METERS = 500` m each way. The
   distance band, the 10% repetition limit, the two-route minimum and the
   70% overlap dedupe are unchanged. The longer connector is drawn and
   measured as part of the route, so its signals, crossings and repetition
   count like the rest.

`grid.neighbouring_cells` takes an optional radius; radius 1 returns the same
nine cells in the same order. The API's response shape is unchanged. A
retried hit logs `pool_retry` at debug level, and `compare_pools.py` counts
those.

## Before and after

Two alternating before/after runs of the 600-request check, sequential on
one laptop, `--no-live`. Both runs gave the same hit counts; timings are
the two runs' values.

| Measure | Before | After |
|---|---:|---:|
| Hits | 548 / 600 (91.3%) | **582 / 600 (97.0%)** |
| Mixed / road / trail | 184 / 181 / 183 | 194 / 194 / 194 |
| 3 / 5 / 6.5 / 8 / 12 mi | 107 / 108 / 111 / 114 / 108 | 117 / 114 / 117 / 117 / 117 |
| Hits returning two routes | 20 | 24 |
| Median / p95 pool time, all hits | 64-65 / 134 ms | 67 / 139-143 ms |
| Same, on the 548 shared hits | 64-65 / 134 ms | 66 / 139-142 ms |
| Same, on the 34 new hits | (planned live) | 73-74 / 139-143 ms |
| Serving time before a remaining miss falls back, median / p95 | 0 / 7-8 ms | 1 / 20-21 ms |

The 548 shared hits returned identical routes and metrics in both versions.
Their 1 to 2 ms median difference is run-to-run noise on the same code
path. The new hits cost about 8 ms more than an ordinary hit, against a
median of 1,448 ms to plan the same 34 requests live. A request that still
misses spends up to about 20 ms more in serving before planning live.

Quality on hits, as in [validation.md](validation.md): medians over each
request's mean route metric.

| Measure | Before, 548 hits | After, 582 hits | 34 new hits |
|---|---:|---:|---:|
| Median absolute distance error, mi | 0.089 | 0.089 | 0.095 |
| Median major-road crossings per mile | 0.270 | 0.267 | 0.252 |
| Median repeated fraction | 2.0% | 2.1% | 4.5% |
| Median compactness | 0.386 | 0.384 | 0.362 |

The new hits double back more than the typical hit, because they come from
sparse streets and use longer connectors. They stay under the 10% limit
(the worst request averages 9.7%). Compared with live planning on the same
34 requests (`--live-on-retry`), they are no worse on any measure except
signals:

| On the 34 new hits | Pool, after | Live |
|---|---:|---:|
| Median absolute distance error, mi | 0.095 | 0.131 |
| Median major-road crossings per mile | 0.252 | 0.365 |
| Median repeated fraction | 4.5% | 4.8% |
| Median compactness | 0.362 | 0.361 |
| Median signals per mile | 0.196 | 0.169 |

The wider retry's 19 compare 5.3% repeated against live's 6.3%. The
connected-start 15 compare 2.8% against 3.7%.

## The 18 that still miss

- 15 from start 37: no clean loop within two rings. Live planning there
  returns a median 38% repeated distance, compactness 0.04 and a 0.33 mi
  distance error, so falling back is right.
- 3 from start 36, 5 mi on each surface. Only five nodes lie within 300 m.
  The nearest in-band 5 mi loop is 986 m round trip, and the 500 m retry
  joins loops that land out of band. Live gives 28% repeated and a 0.50 mi
  distance error.

One-route acceptance would answer none of these. A pool recompute could
help start 36's cell, for example a second origin in sparse cells. Joining
loops partway along an edge, not only at nodes, could too. Neither is
cheap, and neither was done here.

## Scope

Tests: the full suite passes. New serving tests cover the wider retry, the
5x5 ring, the connected start and an unmoved start on the connected graph.
They also check that a hit on the usual read never runs a retry. No
`osm.py`, `scoring.py` or `models.py` code changed, so `bench.py` and
`shape_audit.py` were not rerun. Nothing was recomputed, deployed or pushed.
Raw JSON is in the ignored `output/pool-misses/`.

## Reproduce

From a checkout, with the graph and pool directories holding the matching
pair (here, symlinks under `output/data/`):

```bash
export RUNLINE_GRAPHS_DIR=$PWD/output/data/graphs RUNLINE_POOLS_DIR=$PWD/output/data/pools
PY="env PYTHONPATH=$PWD/src .venv/bin/python"
# Hit rate, latency and quality on hits (600 requests).
$PY scripts/compare_pools.py --starts 40 --surfaces mixed road trail --no-live --output output/pool-misses/after.json
# The same requests, planning live only where a retry answered or serving missed.
$PY scripts/compare_pools.py --starts 40 --surfaces mixed road trail --live-on-retry --output output/pool-misses/after-live.json
# Miss reasons and counterfactual fixes, without and with the retries.
$PY scripts/pool_misses.py --without-retries --output output/pool-misses/diagnosis-before.json
$PY scripts/pool_misses.py --output output/pool-misses/diagnosis-after.json
```

For the "before" column, run the first command from a checkout of the parent
commit (`git archive c4864b3 src scripts` into a scratch directory, then run
its `scripts/compare_pools.py` with `PYTHONPATH` set to its `src`).
