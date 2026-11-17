# Acceptance criteria results (2026-11-16)

Measured against the acceptance criteria in the Project Proposal & Plan.
Feeds the QA / Validation Report.

| Item | Value |
|---|---|
| Code measured | `61f45cd2cd6a0a3a4f5bef407f367414716cd4dc` (main `f482881` plus `scripts/acceptance.py`; clean tree) |
| Graph | rebuilt Charlottesville, SHA-256 `66b524897627167e85bcfdf7b7dab996bc92e1e622343988584fd5b83cf573e9` |
| Pools | complete area file, 11,417 pools, SHA-256 `c18a02a0...f03c`, built at `9b56ec5` |
| Pool / graph fingerprint | `66b524897627167e` / `66b524897627167e` (match; both also match the release record) |
| Machine | Apple M4 Pro laptop, 24 GB, macOS, Python 3.13.0, run sequentially |

The data are the staged production pair (graph and complete pool), read in
place and not modified. Production routing code differs from the measured
code only by PR #26's cap on waypoint rescaling (`MAX_RESCALE`), which only
affects degenerate attempts. The production API was not called.

## Result

| Criterion | Threshold | Live | Served | Overall |
|---|---|---:|---:|---:|
| US1 distance | >= 80% of top routes within 5%; all within 10% | PASS (90.6%, 32/32 within 10%) | PASS (100%, 32/32) | **PASS** |
| US2 alternatives | >= 80% of requests with a pair overlapping <= 80% | PASS (32/32) | PASS (31/32, 96.9%) | **PASS** |
| US3 interruptions | median signals + crossings of top route down >= 20%, median error <= 5% | PASS (-71.4% proxy, -76.9% geometric, 1.45% error) | PASS (-78.6% / -76.9%, 0.95%) | **PASS** |
| US5 surface | >= 75% of applicable cases toward the preference | PASS (62/64, 96.9%) | PASS (63/64, 98.4%) | **PASS** |
| US6 elevation | hilly gain > flat gain in >= 80% of pairs | **FAIL (21/32, 65.6%)** | PASS (26/32, 81.2%) | **PARTIAL** |
| US7 GPX | 10 sampled GPX parse, validate, length within 1% | PASS (10/10) | PASS (10/10) | **PASS** |
| US10 latency (local) | warm 3/5 mi median < 10 s, p95 < 20 s | PASS (0.33 s / 1.18 s) | PASS (0.07 s / 0.36 s) | **PASS** |

US6 fails on the live planner and passes on the served path by one case.
Nothing was tuned to change any result.

## Method

**Suite.** The eight public starts `scripts/bench.py` builds (the Rotunda,
the map centre from the manifest, and `ring_000` ... `ring_300`, 2.5 km from
the centre every 60 degrees), each at 3, 5, 8 and 12 miles: 32 requests. Three
routes per request, no drive radius. Private starts are never read. Each
request runs in five variants: mixed/balanced (`base`, used for US1, US2, US3
and US10), mixed/flat and mixed/hilly (US6), road/balanced and trail/balanced
(US5).

**Modes.** Every request is planned twice on the same graph:

- *live*: `planner.plan_routes` with pools switched off. Deterministic; this
  is the benchmark planner.
- *served*: `planner.plan_routes` exactly as `/api/routes` calls it, pools
  first and live planning when no pool answers. This is what users get. The
  pools answered 30/32 base requests; `ring_120 3 mi` and `ring_180 5 mi`
  fell back to live.

A request is *successful* when it returns at least one route. All 160
requests in each mode succeeded. Repeated requests returned the same top
route (checked on every timing repeat).

**Coverage.** Every 12 mile request except the map centre's (7 of 8) needs
more map than the 12 km area holds and is marked `coverage_limited`; the live
planner warned on all seven. `ring_120 12 mi` live also warned that no route
fell within the ±0.25 mi band (top route 11.73 mi, 2.2% off).

**Definitions.**

- *Error %* = |route miles - requested miles| / requested miles. US1 uses the
  top-ranked route; "all within 10%" is also judged on the top route.
- *Overlap* (US2) = shared undirected edge length / the shorter route's
  unique edge length. This is stricter than the planner's own duplicate test
  (shared / union, rejected at >= 0.70), which is also recorded. Applicable =
  every successful request (all 32); a request passes if any returned pair is
  <= 0.80.
- *Interruptions* (US3) = mapped traffic-signal events + major-road
  crossings, counted two ways: the planner's own crossing count (*proxy*,
  `osm._major_crossings`) and the independent geometric labels of
  `scripts/audit_crossings.py` (*geometric*). Signals are the planner's signal
  events in both. US3 passes only if both counts pass.
- *Distance-only baseline* (US3): the same planner on the same requests with
  pools off, path cost = edge length only (no signal, major-road,
  crossing-node or surface penalty) and ranking by absolute distance error
  only (no interruption, shape, repetition or elevation terms). Implemented in
  the script by patching `osm.collapse_graph` and both `rank_candidates`
  references in-process, with the prepared-graph cache cleared on entry and
  exit. Waypoint sampling and the distinct-route filter are unchanged.
  Production code is not modified.
- *Toward the surface* (US5): trail request's top-route trail fraction >= the
  mixed request's; road request's <= the mixed one's. Applicable when the
  mixed top route has both surfaces (0 < trail fraction < 1): 32/32 in both
  modes, so 64 comparisons per mode. Strict rates exclude ties.
- *Elevation* (US6): top-route gain from the graph's baked Copernicus
  elevation (100% node coverage), pairs where both routes have >= 95%
  elevation coverage (all 32). A tie (equal gain, in practice the same route)
  does not count as greater.
- *GPX* (US7): `gpx.candidate_to_gpx` as the API calls it, for every returned
  route; ten per mode sampled with seed 4971 across all variants. Checks:
  Python `xml.etree` parse; validation against the official GPX 1.1 XSD
  (downloaded from topografix.com) with `xmllint` (libxml2 2.13.9), because
  `lxml` is not installed in the project venv; root namespace and version, one
  `trk` and `trkseg`, every `trkpt` lat/lon in range and equal to the route
  geometry, first point == last point; track length (haversine over the
  points) within 1% of the reported distance.
- *Warm latency* (US10): wall time of `plan_routes` in one process after a
  first, excluded request has loaded the graph and checked the pool. For
  each start and 3/5 mi the prepared-graph cache is cleared, then the request
  runs three times: repeat 0 is a new start on a warm instance, repeats 1-2
  reuse its prepared graph. 48 samples per mode. **These are laptop
  measurements, not Vercel or Oracle timings.**

## US1 distance accuracy (mixed, balanced)

| Mode | Successful | Top within 5% | Rate | Top within 10% | Median error | Max error |
|---|---:|---:|---:|---:|---:|---:|
| live | 32/32 | 29 | 90.6% | 32/32 | 1.45% | 7.87% |
| served | 32/32 | 32 | 100.0% | 32/32 | 0.95% | 4.59% |

Live top routes outside 5%: `ring_000 3 mi` (6.0%), `ring_240 3 mi` (7.9%),
`ring_300 3 mi` (6.6%). All three are inside the product's ±0.25 mi band,
which is ±8.3% at 3 miles, so the 5% criterion is tighter than the band at
short distances. Within the band, ranking weighs distance at 0.15 of its
out-of-band weight on purpose (`docs/routing-study.md`), trading a few
hundredths of a mile for fewer interruptions.

## US2 distinct alternatives

| Mode | Applicable | Returned >= 2 | Pair <= 80% | Rate | Pair <= 80%, both in band | Median lowest pair overlap | Max lowest pair overlap |
|---|---:|---:|---:|---:|---:|---:|---:|
| live | 32 | 32 | 32 | 100.0% | 31 | 0.097 | 0.404 |
| served | 32 | 32 | 31 | 96.9% | 31 | 0.181 | 0.814 |

The served miss is `ring_180 3 mi`: the pool returned only two routes, and
they share 81.4% of the shorter one's length. The planner kept both because
its duplicate test divides by the union (0.667, under its 0.70 limit). When
two routes differ in length, a union ratio under 0.70 can still exceed 0.80
of the shorter route, so the code's measure and the proposal's measure can
disagree.

## US3 interruptions vs the distance-only baseline

| Mode | Crossings | Pairs | Baseline median (top) | Aware median (top) | Reduction (top) | Baseline median (all) | Aware median (all) | Reduction (all) | Total top | Fewer/equal/more | Aware median error | Verdict |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| live | proxy | 32 | 14.0 | 4.0 | 71.4% | 14.0 | 3.0 | 78.6% | 507 -> 108 | 28/3/1 | 1.45% | PASS |
| live | geometric | 32 | 13.0 | 3.0 | 76.9% | 13.0 | 3.0 | 76.9% | 458 -> 105 | 28/3/1 | 1.45% | PASS |
| served | proxy | 32 | 14.0 | 3.0 | 78.6% | 14.0 | 3.0 | 78.6% | 507 -> 98 | 29/1/2 | 0.95% | PASS |
| served | geometric | 32 | 13.0 | 3.0 | 76.9% | 13.0 | 3.0 | 76.9% | 458 -> 94 | 29/1/2 | 0.95% | PASS |

"All" pools every returned route (up to 96 per side). The baseline's median
top-route error is 0.61%: it fits distance more tightly, at about 3.5 times
the interruptions. The served rows compare pool-first serving with the live
baseline.

| Miles | Proxy, baseline -> aware | Geometric | Error %, baseline -> aware |
|---|---:|---:|---:|
| 3 | 4.5 -> 1.0 | 4.0 -> 1.0 | 0.38 -> 2.98 |
| 5 | 10.5 -> 2.5 | 10.5 -> 2.5 | 0.74 -> 1.50 |
| 8 | 17.0 -> 4.0 | 14.0 -> 4.0 | 0.29 -> 1.38 |
| 12 | 32.0 -> 5.5 | 28.0 -> 5.0 | 0.82 -> 0.93 |

Live, per-distance top-route medians. Aware ranking was worse on
`ring_060 3 mi` (2 crossings against the baseline's 1 signal, in both modes)
and, served, on `ring_180 3 mi` (2 signals + 4 proxy / 2 geometric crossings
against none).

## US5 surface preference

| Mode | Request | Applicable | Toward or tie | Strictly toward | Ties | Rate | Strict rate | Median trail fraction, mixed -> requested |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| live | trail | 32 | 31 | 30 | 1 | 96.9% | 93.8% | 0.50 -> 0.85 |
| live | road | 32 | 31 | 31 | 0 | 96.9% | 96.9% | 0.50 -> 0.14 |
| live | both | 64 | 62 | 61 | 1 | 96.9% | 95.3% | |
| served | trail | 32 | 31 | 31 | 0 | 96.9% | 96.9% | 0.48 -> 0.83 |
| served | road | 32 | 32 | 32 | 0 | 100.0% | 100.0% | 0.48 -> 0.16 |
| served | both | 64 | 63 | 63 | 0 | 98.4% | 98.4% | |

Moved away from the preference: live trail `Rotunda 12 mi` (0.44 -> 0.43)
and live road `ring_120 5 mi` (0.30 -> 0.54); served trail `ring_120 8 mi`
(0.60 -> 0.44).

## US6 elevation preference: FAIL on live, PASS on served

| Mode | Pairs | Hilly greater | Ties | Hilly less | Rate | Median gain flat / balanced / hilly, ft | Verdict |
|---|---:|---:|---:|---:|---:|---:|---:|
| live | 32 | 21 | 11 | 0 | 65.6% | 581 / 636 / 646 | FAIL |
| served | 32 | 26 | 6 | 0 | 81.2% | 518 / 529 / 663 | PASS |

Hilly never produced less gain than flat; every miss is a tie where both
settings pick the same top route. Live ties: `ring_060 5`, `ring_120 3`,
`ring_120 12`, `ring_180 3/5/8/12`, `ring_240 5/8/12`, `ring_300 3` mi.
Served ties: `map centre 5`, `ring_000 3`, `ring_060 8`, `ring_120 3`,
`ring_120 5`, `ring_180 5` mi (two of these were live fallbacks). Counting
ties as passes would give 100% in both modes, but the criterion says
"greater".

Why: elevation preference is a ranking term only. Path costs ignore it, so
flat and hilly search the same candidates. It has the smallest weight (1.5),
applied to the route's gain normalised to 0..1 across the candidate set,
while signals (6 per signal per mile), crossings (4), repetition (5) and shape
(4) also count, and in-band, non-sliver routes always rank first. A small
elevation term rarely overturns them. In several ties, the hilly ranking puts
a lower-gain route first even though its own second and third routes climb
more with the same interruptions (for example `ring_120 3 mi`: 231 ft first,
372 and 414 ft below it, all with zero signals and crossings), so shape,
repetition and distance decide. Serving passes because a pool request
re-ranks loops from nine cells, a larger set with more varied gain, but it
clears 80% by one pair. Any fix (a larger elevation weight, or elevation in
the path cost) trades against the interruption ranking that US3 measures and
needs its own paired benchmark; this run tuned nothing.

## US7 GPX validity

| Mode | Set | Routes | XML parses | XSD valid | Structure ok | Closed | Length within 1% | Max length error | All checks |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| live | sample | 10 | 10 | 10 | 10 | 10 | 10 | 0.000% | 10 |
| live | all | 576 | 576 | 576 | 576 | 576 | 576 | 0.000% | 576 |
| served | sample | 10 | 10 | 10 | 10 | 10 | 10 | 0.000% | 10 |
| served | all | 474 | 474 | 474 | 474 | 474 | 474 | 0.000% | 474 |

The largest length difference was 3e-6%. Reported distance sums the graph's
edge lengths, which OSMnx computes by great circle over the same geometry the
GPX writes. The check therefore confirms that no points were dropped or
duplicated; it is not an independent distance measurement. The smallest
track had 146 points.

## US10 warm latency, 3 and 5 mi (laptop)

| Mode | Samples | Median s | p95 s | Max s | New start median / p95 | Repeat median / p95 | Pool hits median (n) | Live median (n) | Verdict |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| live | 48 | 0.326 | 1.175 | 1.243 | 0.771 / 1.206 | 0.244 / 0.377 | n/a (0) | 0.326 (48) | PASS |
| served | 48 | 0.071 | 0.357 | 1.118 | 0.085 / 0.509 | 0.062 / 0.245 | 0.054 (42) | 0.345 (6) | PASS |

Process cold start, excluded: graph load 0.16 s, first live request 1.19 s,
first served request 0.30 s. Both modes sit well below the 10 s / 20 s
thresholds on this laptop. Serverless cold starts, network, and slower CPUs
are not covered; the deployed system is measured separately.

## Per-request results (mixed, balanced; top route)

Signals + proxy crossings, geometric crossings in brackets.

| Start | mi | Live mi | Live err | Live sig+cross (geom) | Served src | Served mi | Served err | Served sig+cross (geom) | Baseline mi | Baseline err | Baseline sig+cross (geom) | Warnings |
|---|---:|---:|---:|---:|---|---:|---:|---:|---:|---:|---:|---|
| Rotunda | 3 | 2.93 | 2.4% | 0+3 (3) | pool | 3.14 | 4.6% | 0+2 (2) | 2.98 | 0.6% | 8+6 (5) |  |
| Rotunda | 5 | 5.13 | 2.6% | 0+4 (4) | pool | 5.18 | 3.7% | 0+2 (2) | 5.00 | 0.1% | 9+5 (5) |  |
| Rotunda | 8 | 7.89 | 1.3% | 1+2 (2) | pool | 8.01 | 0.2% | 1+2 (2) | 8.00 | 0.0% | 15+4 (3) |  |
| Rotunda | 12 | 12.09 | 0.8% | 1+5 (5) | pool | 12.11 | 0.9% | 1+5 (5) | 11.91 | 0.8% | 28+9 (7) | coverage_limited |
| map centre | 3 | 2.89 | 3.6% | 0+2 (2) | pool | 2.97 | 1.0% | 0+2 (2) | 2.99 | 0.4% | 3+4 (4) |  |
| map centre | 5 | 5.00 | 0.1% | 0+1 (1) | pool | 4.91 | 1.7% | 0+1 (1) | 5.04 | 0.7% | 10+5 (4) |  |
| map centre | 8 | 8.00 | 0.0% | 2+3 (3) | pool | 7.94 | 0.7% | 2+2 (1) | 7.94 | 0.7% | 5+8 (8) |  |
| map centre | 12 | 11.87 | 1.1% | 1+3 (3) | pool | 11.99 | 0.1% | 1+3 (3) | 12.23 | 1.9% | 21+18 (9) |  |
| ring_000 | 3 | 3.18 | 6.0% | 0+0 (0) | pool | 2.95 | 1.7% | 0+0 (0) | 2.98 | 0.6% | 3+2 (1) |  |
| ring_000 | 5 | 5.12 | 2.4% | 1+0 (0) | pool | 5.05 | 1.1% | 0+0 (0) | 5.02 | 0.5% | 2+3 (5) |  |
| ring_000 | 8 | 7.77 | 2.8% | 0+4 (4) | pool | 7.97 | 0.3% | 1+2 (2) | 8.08 | 1.0% | 7+9 (3) |  |
| ring_000 | 12 | 11.79 | 1.8% | 2+3 (3) | pool | 11.95 | 0.4% | 2+2 (2) | 12.08 | 0.6% | 19+11 (7) | coverage_limited |
| ring_060 | 3 | 2.94 | 2.1% | 0+2 (2) | pool | 2.98 | 0.5% | 0+2 (2) | 3.01 | 0.2% | 1+0 (0) |  |
| ring_060 | 5 | 5.04 | 0.7% | 0+2 (2) | pool | 5.00 | 0.1% | 0+2 (2) | 5.04 | 0.8% | 5+4 (4) |  |
| ring_060 | 8 | 8.13 | 1.6% | 1+0 (0) | pool | 8.08 | 1.0% | 0+0 (1) | 8.16 | 2.0% | 15+7 (5) |  |
| ring_060 | 12 | 12.18 | 1.5% | 2+2 (1) | pool | 12.06 | 0.5% | 2+2 (1) | 11.84 | 1.3% | 26+8 (7) | coverage_limited |
| ring_120 | 3 | 2.99 | 0.2% | 0+0 (0) | live | 2.99 | 0.2% | 0+0 (0) | 2.99 | 0.2% | 0+0 (0) |  |
| ring_120 | 5 | 5.05 | 1.1% | 1+2 (2) | pool | 5.02 | 0.4% | 0+2 (2) | 5.04 | 0.8% | 3+0 (0) |  |
| ring_120 | 8 | 7.94 | 0.7% | 1+3 (3) | pool | 8.24 | 3.0% | 0+1 (1) | 8.00 | 0.0% | 4+3 (2) |  |
| ring_120 | 12 | 11.73 | 2.2% | 0+5 (5) | pool | 11.97 | 0.2% | 1+2 (2) | 12.10 | 0.9% | 17+5 (3) | coverage_limited |
| ring_180 | 3 | 2.99 | 0.4% | 0+0 (0) | pool | 2.90 | 3.4% | 2+4 (2) | 2.99 | 0.4% | 0+0 (0) |  |
| ring_180 | 5 | 5.09 | 1.9% | 3+3 (3) | live | 5.09 | 1.9% | 3+3 (3) | 4.93 | 1.3% | 4+4 (3) |  |
| ring_180 | 8 | 8.08 | 1.1% | 4+4 (4) | pool | 8.22 | 2.8% | 3+5 (5) | 8.00 | 0.1% | 5+7 (7) |  |
| ring_180 | 12 | 12.04 | 0.4% | 2+4 (4) | pool | 12.04 | 0.4% | 2+4 (4) | 12.07 | 0.6% | 15+11 (9) | coverage_limited |
| ring_240 | 3 | 2.76 | 7.9% | 0+0 (0) | pool | 3.08 | 2.7% | 0+0 (0) | 3.00 | 0.2% | 2+2 (2) |  |
| ring_240 | 5 | 5.01 | 0.2% | 0+5 (5) | pool | 4.92 | 1.6% | 0+2 (2) | 5.07 | 1.3% | 7+5 (5) |  |
| ring_240 | 8 | 8.12 | 1.4% | 0+4 (4) | pool | 8.12 | 1.4% | 0+4 (4) | 8.02 | 0.3% | 11+7 (4) |  |
| ring_240 | 12 | 12.07 | 0.6% | 1+5 (4) | pool | 11.90 | 0.8% | 1+5 (4) | 11.93 | 0.5% | 17+8 (5) | coverage_limited |
| ring_300 | 3 | 2.80 | 6.6% | 0+2 (2) | pool | 3.00 | 0.1% | 1+2 (2) | 2.98 | 0.6% | 5+3 (1) |  |
| ring_300 | 5 | 5.19 | 3.8% | 0+1 (1) | pool | 4.77 | 4.6% | 0+1 (1) | 4.99 | 0.3% | 8+6 (6) |  |
| ring_300 | 8 | 8.22 | 2.8% | 1+3 (2) | pool | 8.16 | 2.0% | 1+4 (4) | 8.02 | 0.3% | 18+9 (4) |  |
| ring_300 | 12 | 12.03 | 0.3% | 1+6 (6) | pool | 12.07 | 0.6% | 1+5 (5) | 11.90 | 0.9% | 29+12 (8) | coverage_limited |

## Limits

- One city and 32 requests. Rates are counts out of 32 (or 64 for US5), so
  one case moves a rate by about 3 points.
- Crossing labels and signals come from the same OSM map data, so the
  geometric count is an independent reading of those data, not field truth.
- The US3 baseline keeps RunLine's waypoint generator and distinct-route
  filter; only path costs and ranking are distance-only.
- The served path's pool answers do not report coverage warnings; the
  coverage column above comes from the live planner and the graph crop.

## Reproduce

```bash
PYTHONPATH=$PWD/src .venv/bin/python scripts/acceptance.py --data <release dir with graphs/ and pools/>
PYTHONPATH=$PWD/src .venv/bin/python -m pytest -q
```

The run takes a few minutes on the laptop above. It refuses to measure if
the pool's graph fingerprint does not match the graph. Output goes to the
git-ignored `output/acceptance/`: `results.json` (per request and mode:
distances, errors, signals, both crossing counts, trail fraction, gain,
overlap pairs, timing, source, warnings, GPX checks; no coordinates),
`summary.md` (these tables), the 20 sampled GPX files and the downloaded XSD.
`--starts` and `--miles` run a subset for a quick check.
