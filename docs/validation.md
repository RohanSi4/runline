# Recovery validation

The November 10 recovery integrates the five local Claude branches on
`codex/resume-runline`, including upstream Oracle hosting commit `32a7876`.
The production Vercel API proxy is unchanged. The baseline checks ran on
`a92d3a9`; its routing code and shipped graph are identical to `32a7876`.

## Checks

- Full integrated suite: **180 passed**, including the PBF builder tests.
  Python 3.13.0; networkx 3.6.1, numpy 2.5.2, shapely 2.1.2, osmnx 2.1.1,
  and osmium 4.3.2. Osmium came from the prior session's local installation;
  without that optional dependency the PBF tests are skipped.
- Shell syntax: all 16 deployment/SLURM shell, job and environment files
  passed `bash -n`.
- Browser script: `node --check web/app.js` passed. A Node VM smoke check
  confirmed string and structured FastAPI errors render as text and the
  distance input is required. This is not a visual browser check.
- Real `b6` plan with Washington included: **493,968 cells**, 10,666 units,
  no duplicate cells, and every used tile present in the serving index.
- Production: `/api/health` returned `ok`, `/api/areas` listed Charlottesville,
  and a five-mile mixed-surface request from the public map center returned
  **`source: pool` with three routes**. No backend deployment was performed.

## Live-routing benchmark

Fresh, sequential before/after runs of `scripts/bench.py --repeat 3` on the
same shipped Charlottesville graph: eight public starts at 3, 5, 8 and 12
miles, 32 cases and 96 returned routes. The explicit private-start file added no extra starts to this set.

| Measure | Baseline | Integrated |
|---|---:|---:|
| Routes within requested ±0.25 mi | 91 / 96 | 91 / 96 |
| Mean absolute distance error, mi | 0.1249 | 0.1267 |
| Total signal events | 45 | 38 |
| Total geometric major-road crossings | 265 | 257 |
| Mean repeated fraction | 4.39% | 4.65% |
| Median warm end-to-end time | 374 ms | 393 ms |

The warm-time median is over each case's median of repetitions 2 and 3.
Timings are laptop measurements, not an Oracle load test. Signal counts
reflect the new event grouping and changed route choices; the shipped graph
still lacks the crossing tags and nodes that the new builders will retain.
These numbers do not measure the benefit of rebuilding that graph.

## Shape audit

Fresh runs of `scripts/shape_audit.py`, 96 routes each. Its ring-start
construction differs slightly from `bench.py`, so its route counts should
be compared within this table rather than joined to the benchmark rows.

| Measure | Baseline | Integrated |
|---|---:|---:|
| Compactness below 0.20 | 9 | 9 |
| Compactness below 0.35 | 35 | 37 |
| Median compactness | 0.3893 | 0.3891 |
| Routes touching a dead-end node | 0 | 0 |
| Routes with a short revisit | 0 | 0 |
| Routes repeating over 10% | 11 | 13 |

The measurement fixes do not improve every aggregate: two additional routes
exceed 10% repetition in the shape audit. Distance fit and the sampled
dead-end/revisit counts are unchanged. Pool serving still rejects routes
above the repetition threshold; live planning can return the closest
available options.

## Pool serving

A fresh comparison used 40 seeded public starts and five distances (200
requests), with the prior session's regenerated mixed-surface pools for 278
sampled cells. The code remeasures the complete route, connectors included.
The sample is not the complete production pool file, and the graph has not
been rebuilt for the new crossing tags.

| Measure | Live | Pool |
|---|---:|---:|
| Requests answered | 200 / 200 | 192 / 200 (96%) |
| Median time | 1,409 ms | 76 ms |
| Median absolute distance error, mi | 0.104 | 0.083 |
| Median major-road crossings per mile | 0.274 | 0.208 |
| Median repeated fraction | 2.40% | 2.28% |
| Median compactness | 0.391 | 0.391 |

Quality medians are over each request's mean route metric, matched on the
192 hits. Live latency covers all 200 requests; pool latency covers hits.
Pool p95 was 121 ms. Both sources' median signals per mile were zero on the
matched sample; this says little about signals absent from the old graph.

The old serving code and old sample pools were rerun on the same starts:
**113 / 200 hits (56.5%)**, versus **192 / 200 (96%)** after the generation
and serving changes. The earlier handoff's 71% came from code before the
geometric doubling-back filter; it is not the current-main baseline.
The fresh integrated run's eight misses comprised six unreachable-loop
requests and two requests with too few loops in band. It still returns
three routes on a hit; no result-count API change was made.

On the complete rebuilt-graph pools, serving's two miss retries (a start
moved onto the connected graph, then a 5x5 ring with a 500 m connector)
raised the 600-request three-surface check from 548 to 582 hits (97.0%);
see [pool-misses.md](pool-misses.md).

Rivanna `b6` and CS two-tile smoke submission scripts also completed local
`DRY_RUN=1` runs. The empty `done` list works; the Rivanna output includes
Washington, uses the standard partition and allocation flag, and chains the
build, four precompute arrays and merge. These runs submitted no jobs.

## Scope

Actual DC tile graphs, the full production pool files, a production graph
rebuild, Oracle updates, Azure provisioning and Rivanna execution were not
performed. The optional VM recipe was previously tested by Claude in an
Ubuntu 24.04 systemd container; this recovery checked its syntax and
integration only. The next deployment should update the existing Oracle
backend, whose service path and port differ from the optional recipe.

Raw benchmark JSON and logs are kept locally under
`output/recovery-validation/`, which is gitignored. They contain no stored
start coordinates; the sampled pool files remain private local artifacts.

## Reproduce

Use the repository's pinned runtime and install its build/dev dependencies
for the graph-builder tests. Place an old source checkout at
`/tmp/runline-baseline` for the paired benchmark. Run commands sequentially
when comparing timing:

```bash
.venv/bin/python -m pytest -q
.venv/bin/python scripts/bench.py --source-root /tmp/runline-baseline --repeat 3 --output output/bench-before.json
.venv/bin/python scripts/bench.py --repeat 3 --output output/bench-after.json
.venv/bin/python scripts/shape_audit.py
RUNLINE_POOLS_DIR=<sample-pool-directory> .venv/bin/python scripts/compare_pools.py --starts 40 --output output/pool-comparison.json
```

Run the baseline's `scripts/shape_audit.py` from its own checkout too. The
pool comparison's default sample is seed 7, starts within 6 km of the map
center, mixed surface, at 3, 5, 6.5, 8 and 12 miles. To regenerate its sample
pools, first use `compare_pools.py --write-cells`, then pass that cell list
to `precompute_pools.py --cells` before merging the shards.
