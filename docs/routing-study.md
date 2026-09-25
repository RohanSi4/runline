# Charlottesville routing study (2026-09-24)

The baseline is commit `c9a25de` on the same shipped walk graph. The new
implementation was compared at the Rotunda and at the center recorded in
`graphs/manifest.json`, with downloads disabled, mixed surface, balanced
elevation, no drive radius, and three routes per request. Each timing is the
median of three runs in one process; the reported p95 uses only three samples
and should be treated as descriptive. Optional private Charlottesville starts
were absent and are listed as skipped by the benchmark. Ashburn has no shipped
graph and is skipped.

| Origin | Miles | Feasible top 3, before→after | Major-crossing proxy total | Signal total | Mean repeated fraction | Generation p50, seconds |
| --- | ---: | --- | --- | --- | --- | --- |
| Rotunda | 3 | 3→3 | 12→7 | 0→0 | .032→.017 | .961→.619 |
| Rotunda | 5 | 3→3 | 16→10 | 0→0 | .070→.043 | 1.549→1.250 |
| Rotunda | 8 | 3→3 | 23→13 | 0→0 | .110→.075 | 2.253→2.139 |
| Rotunda | 12 | unavailable→3 | —→14 | —→0 | —→.074 | —→2.711 |
| Map center | 3 | 3→3 | 9→5 | 0→0 | .212→.093 | .763→.661 |
| Map center | 5 | 3→3 | 19→7 | 0→0 | .122→.137 | 1.528→1.448 |
| Map center | 8 | 2→3 | 26→9 | 1→0 | .129→.076 | 2.115→2.483 |
| Map center | 12 | 1→3 | 24→10 | 2→4 | .213→.136 | 2.334→2.940 |

The 8/12-mile center cases take longer because the first grid did not find
three distinct routes within ±0.25 mile, so a 48-pair refinement runs. Peak
RSS for the complete benchmark process was 604→618 MB. The extra memory comes
from serving the previously failing 12-mile Rotunda case and cached A* node
positions; these peak figures are not a same-workload memory comparison.

## Decisions backed by experiments

- A 300 m cost for entering a minor edge at a node with major road on two
  sides reduced geometry-labeled intersections on selected top-three routes:
  Rotunda 3/5/8 miles 17→10, 21→15, 30→22; center 3/5/8/12 miles
  12→8, 26→12, 32→13, 27→11. All reductions were at least 20%, with no
  decrease in feasible top-three count. Local geometry labels are not evidence
  of marked pedestrian crossings or traffic control.
- A reproducible 50 counted / 50 uncounted transition sample in
  `tests/fixtures/crossing_audit.json` produced 49 true positives, one false
  positive, and one false negative against local road geometry: .98 precision
  and .98 recall. The false positive was a major-road dead end, which the
  search charge excludes. Nearby nodes for the same named road within 30 m
  and 60 m of route travel are counted as one crossing; Emmet Street and Ivy
  Road remain separate.
- The A* heuristic uses the minimum edge cost divided by its endpoint chord
  length, multiplied by chord distance to the goal. Triangle inequality makes
  it admissible even when stored edge lengths are bad. A* beat bidirectional
  Dijkstra on the smaller 3/5-mile crops, but lost on 8/12-mile crops, so it is
  used only below 30,000 nodes. All 792 base waypoint pairs across the two
  origins and four distances matched Dijkstra's path cost exactly.
- The original top-20 overlap-penalized return search did not change any
  selected Rotunda route and increased latency. Limiting it to selected routes
  with at least 12% repeated length helped: a center 5-mile route changed from
  18.3% to 12.0% repeated length with the same crossing count.
- Ranking now puts in-tolerance routes first and uses a smaller distance
  penalty inside the band. Signal and repetition weights were calibrated
  against actual candidates. Shared *length* deduplication at .70 replaced a
  center 5-mile pair sharing 78% of road length with an option sharing 16%.

An offline 720-pair sweep at each Rotunda distance (2,880 pairs total) and at
the center's 8/12-mile distances found feasible routes at all distances. One
Rotunda 8-mile sweep route dominated the initial request-time winner: same
three crossings and 8.5% repetition, with .08 instead of .22 mile error. The
bounded .65-radius refinement now finds that route. At the other tested
distances, the generated top route is within .05 mile error and one crossing
of the best sweep route, or better on those measures. The sweep estimates an
opportunity set, not an optimum over every possible loop.

## Reproduction

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt pytest
mkdir -p /tmp/runline-baseline
git archive c9a25de | tar -x -C /tmp/runline-baseline
.venv/bin/python scripts/bench.py --source-root /tmp/runline-baseline --repeat 3 --output /tmp/before.json
.venv/bin/python scripts/bench.py --repeat 3 --output /tmp/after.json
.venv/bin/python scripts/audit_crossings.py
.venv/bin/python scripts/sweep.py --origin rotunda --output /tmp/sweep.json
.venv/bin/python -m pytest -q
```

`requirements.txt` now includes Shapely: the shipped `.pkl.gz` cannot unpickle
without it. A production-only environment loaded all 105,020 walk edges after
the dependency was added. The Rotunda 12-mile `plan_routes` call returned three
in-band routes and a limited-coverage warning with downloads disabled.
