# Charlottesville routing study (2026-09-24)

Baseline is commit `c9a25de`. Every run uses the shipped walk graph with
downloads disabled, mixed surface, balanced elevation, no drive radius, and
three routes per request. Origins are the Rotunda, the map centre from
`graphs/manifest.json`, and six public points 2.5 km from the centre every 60
degrees (`ring_000` ... `ring_300`), each at 3/5/8/12 miles: 32 cases. Private
Charlottesville starts were absent and are listed as skipped; Ashburn has no
shipped graph. Quality numbers are deterministic; timings are medians of three
runs in one process on an otherwise idle machine.

## Measuring crossings

The ranking counts "major crossings", so the count has to mean something.
Labels come from `scripts/audit_crossings.py`: the route's 15 m corridor is
cut along every major-road centreline, and a crossing is the route moving from
one piece to another. Same-side turns stay in one piece, dead ends do not cut
it, and grade-separated passes are excluded. The label never looks at the
rule it grades.

The round-1 audit used Shapely's `crosses`, which is also true when a route
touches a road's interior vertex and turns back to the same side, so every
transition through a major-road node was labelled a crossing by construction.
Its 98%/98% result was circular and has been replaced.

Event-level agreement on the 96 top-3 routes of 8 origins x 4 distances:

| Rule | Precision | Recall |
| --- | ---: | ---: |
| Any node touching a major road (`c9a25de`) | .727 | .797 |
| Same, plus same-name merge within 30 m (round 1) | .777 | .797 |
| Side change: approach and exit between different major branches, or leaving a major road on the other side from where it was joined (now `_major_crossings`) | **.916** | **.960** |

Only the side rule meets the agreed 0.9/0.9 bar. The remaining misses are
interchange ramps (`trunk_link` is not major), long walks along a primary
road, and two path crossings that share no node with the road. The
`tests/fixtures/crossing_audit.json` slices keep the rule above 0.9/0.9. Sol's
rule scores .852/.738 on the same fixture, so the test does catch a
regression. At a 60 m corridor the label hid real crossings, e.g. on Rugby
Road at Beta Bridge, where the major-road segment ends nearby.

## Result

Totals over each variant's served cases. "Real crossings" are geometric labels.

| Variant | Cases served | In-band top 3 | Real crossings | Signals | Mean repeated | Mean error, mi |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `c9a25de` | 25 | 40/75 | 279 | 31 | 17.4% | .403 |
| round 1 (Sol) | 32 | 92/96 | 240 | 37 | 11.9% | .116 |
| final | 32 | **96/96** | **200** | **27** | 14.4% | .101 |

On the 25 cases the baseline serves, real crossings fell 279 -> 130 and
in-band routes rose 40 -> 75 of 75.

| Miles | In-band, base -> final | Real crossings, base -> final (served cases) | Signals | Repeated |
| ---: | --- | --- | --- | --- |
| 3 | 15/24 -> 24/24 | 28 -> 22 | 4 -> 7 | .199 -> .153 |
| 5 | 14/24 -> 24/24 | 94 -> 33 | 14 -> 1 | .130 -> .165 |
| 8 | 10/24 -> 24/24 | 134 -> 63 | 11 -> 8 | .188 -> .107 |
| 12 | 1/3 (1 case) -> 24/24 (8 cases) | 23 -> 12 | 2 -> 0 | .213 -> .191 |

The 3 mile signal and 5 mile repetition increases come from replacing
out-of-band routes with in-band ones, which is the agreed ranking order.

TIMING_TABLE

## Decisions, each measured against the final code

Paired over the 32 cases; each row removes or changes one thing.

| Change from final | Real crossings | Signals | Repeated | Error | In-band |
| --- | ---: | ---: | ---: | ---: | ---: |
| none | 200 | 27 | 14.4% | .101 | 96/96 |
| no 300 m crossing charge | 324 | 33 | 13.0% | .105 | 96/96 |
| no 0.65-radius refinement | 212 | 27 | 14.2% | .102 | 96/96 |
| no alternate return | 202 | 27 | 17.1% | .100 | 96/96 |
| in-band distance weight 0 | 198 | 30 | 14.3% | .128 | 96/96 |
| in-band distance weight 0.5 | 225 | 37 | 15.7% | .072 | 96/96 |
| in-band distance weight 1.0 | 243 | 38 | 16.0% | .066 | 96/96 |
| no radius rescaling (fixed refinement only) | 211 | 37 | 13.5% | .117 | 92/96 |
| radius rescaling only | 226 | 47 | 17.3% | .128 | 91/96 |

- **Crossing charge ships.** It cuts real crossings 38% (removing it adds
  crossings in 25 of 32 cases, removes them in 3) with no loss of in-band
  routes, clearing the agreed 20% bar.
- **Radius rescaling ships.** At `ring_180` every first-pass route was
  1.1-4.0x the target; the 720-pair oracle sweep found three in-band loops
  per distance there, eight of nine at radius scale 0.5. Rescaling each direction's closest attempt
  by target/actual, then keeping the fixed refinement, reached 96/96.
- **The in-band distance weight stays at 0.15**: fewest crossings plus signals
  (227, against 228/262/281) at .10 mile mean error.
- **Score weights stay at the baseline** (signals 6, repeats 5). Round 1's
  8/8 traded 9 more crossings for 0.9 points less repetition with the same
  signals; the product's objective is fewer interruptions.
- **A\* was removed.** Middle-leg path costs matched Dijkstra on all 1,152
  tested pairs, but it was not faster across origins:

  | Origin | 3 mi | 5 mi | 8 mi | 12 mi |
  | --- | ---: | ---: | ---: | ---: |
  | Rotunda | 0.61x | 0.82x | 1.12x | 1.75x |
  | map centre | 0.78x | 1.01x | 1.39x | 1.99x |
  | ring_120 | 1.12x | 1.71x | 2.05x | 1.97x |

  A* / bidirectional Dijkstra time. Node count does not predict the winner
  (ring_120 loses at 6.4k nodes); the round-1 "A* below 30k nodes" rule
  cost 13.5 s against 12.7 s for plain Dijkstra over these 12 cases.
- **Alternate return ships.** It lowers repetition 17.1% -> 14.4% for top
  routes above 12% repeated, without adding crossings or signals.

## Oracle

`scripts/sweep.py` samples 720 waypoint pairs per distance (radius scales
0.5-1.25, 15 degree headings, five widths) and ranks them with the same
scorer. It estimates what is reachable, not a global optimum. At the Rotunda
and `ring_000`, the generator's top routes are within about one crossing of
the sweep's. The `ring_180` gap it exposed is what radius rescaling closed.

## Coverage and production

A 12 mile request from the Rotunda needs a 10.9 km crop that overhangs the
12 km shipped disc. `load_graph` now serves the part of the map that is there
and marks it `coverage_limited`, and the planner warns. The planner test
`test_rotunda_12_miles_is_served_with_limited_coverage` checks three in-band
routes and the warning. In a fresh venv built from `requirements.txt` alone
(networkx 3.6.1), the graph loads all 105,020 edges and a 12 mile Rotunda plan
returns 11.99/12.22/12.25 miles in 6.0 s cold. With Shapely blocked, the
pickle fails to load, so Shapely stays in `requirements.txt`.

## Reproduction

```sh
.venv/bin/python scripts/audit_crossings.py          # label audit, rewrites the fixture
mkdir -p /tmp/runline-baseline && git archive c9a25de | tar -x -C /tmp/runline-baseline
.venv/bin/python scripts/bench.py --source-root /tmp/runline-baseline --repeat 3 --output /tmp/before.json
.venv/bin/python scripts/bench.py --repeat 3 --output /tmp/after.json
.venv/bin/python scripts/sweep.py --origin ring_180 --distances 3 5 8
.venv/bin/python -m pytest -q
```
