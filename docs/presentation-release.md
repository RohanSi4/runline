# Presentation release validation

The integration from PR #5 is merged. PR #6 adds reproducible replacement
artifacts, release verification, an offline saved example, locally served
Leaflet, and support for returning two qualifying pool routes on the default
up-to-three request. Other result counts retain their existing behavior.

The full suite passes **186 tests** on the rebuilt graph with its matching
crossing fixture. The new two-route cases confirm that one qualifying loop
still falls back to live planning, and two qualifying loops return a clear
notice without relaxing distance, repetition or diversity checks.

## Replacement graph

| Measure | Shipped graph | Rebuilt graph |
|---|---:|---:|
| Walk graph nodes | 40,209 | 40,422 |
| Signal nodes | 131 | 569 |
| Signalized crosswalk nodes retained as signals | 0 | 301 |
| Elevation coverage | Previously baked | 100% |
| Graph hash prefix | c24a3d116d950916 | 66b524897627167e |

The rebuild used cached OSM responses and Copernicus terrain tiles. It
preserves signal nodes through simplification and records the graph build's
networkx/shapely/osmnx/rasterio versions. Existing serving artifacts were
not overwritten. Graphs and pools are staged as matching pairs.

## Graph benchmark

Same eight public starts, four distances, three repetitions, 32 cases.
Before is the integrated routing code on its old graph; after is the same
routing code on the replacement graph. Runs were completed before launching
the long pool builds.

| Measure | Before | After |
|---|---:|---:|
| In-band routes | 91/96 | 93/96 |
| Mean absolute distance error, mi | 0.1267 | 0.1236 |
| Signal events | 38 | 74 |
| Geometric crossings | 257 | 265 |
| Mean repeated fraction | 4.65% | 4.65% |
| Median warm end-to-end time | 393 ms | 384 ms |
| Shape audit compactness <0.20 | 9 | 7 |
| Shape audit short revisits | 0 | 3 |
| Shape audit dead ends | 0 | 0 |
| Shape audit repetition >10% | 13 | 12 |

More counted signals reflect corrected map data and changed route choices,
not an improvement in signal avoidance. Shape results remain mixed, including
one poor fallback option away from the rehearsed starts. Do not claim zero
backtracking or globally optimal routes. The independent crossing audit
reported precision **0.974**, recall **1.000** on 264 matched events, seven
false positives and zero misses. A separate fixture was generated for the
new topology; the old graph's fixture remains intact.

## Pools and rehearsal

The prioritized sample contains **315 mixed-surface pools and 29,849 loops**
(4.5 MB), covering the benchmark's cells plus the prefilled/map-center demo
starts. It is explicitly labeled a sample; other covered requests plan live.
The full area build uses 96 resumable shards and six workers, from fixed
source commit `9b56ec5`; it runs separately from ongoing code commits.

On the same 40-start, five-distance rebuilt-graph sample, allowing two
qualifying routes increased hits **175/200 (87.5%) → 184/200 (92%)**. Nine
hits returned two routes. Signals, crossings, connectors, repetition, distance
and route diversity are still measured and filtered. Timing for this sample
was measured while the long builds ran and should not be treated as an
unloaded performance benchmark.

The prior **56.5% → 96%** study used the original graph and its original
start set. It is preserved in `validation.md` and is not substituted for this
new graph's result.

Ten scenarios on the original production app, and ten on the rebuilt local
API, each returned **30 closed in-band routes with matching GPX tracks**.
The rebuilt sample then answered seven of the ten local scenarios from pools;
road, trail and nearby-start requests without those sample pools planned live.
Version checks distinguish the actual backend from the proxied frontend.
The final public deployment must pass the same rehearsal on its pinned URL.

The standalone HTML fallback embeds actual captured routes and OSM street
segments. It is clearly labeled as recorded, works without network requests,
and offers route selection and GPX downloads. Its JavaScript syntax was
checked. Browser interaction/visual QA was unavailable in this session.

Raw reports and captures are in gitignored `output/presentation/`. Generated
coordinates, graph files and pool files are not committed with the tooling.


## Public deployment verification

The public URL **https://runline-capstone.vercel.app** passed the complete
rehearsal on source commit `77f6c3e14fb1816338dd71477fc8069ffa14b6d2`.
All 30 returned routes closed and fell within their requested distance band;
all GPX points matched the displayed routes. Seven of ten scenarios used the
sample pools; road/trail and nearby-start scenarios used live planning.
The health, backend revision, coverage, autocomplete, local frontend assets,
unsupported-start and invalid-distance checks passed. The standalone saved
example is available at `/assets/saved-demo.html` without API requests.

This is a separate Vercel project. The original `runline-nine` site and its
Oracle proxy were preserved. The signed-in-only preview on the original
project is not the presentation URL.

## Portable artifacts

`package_presentation.py` checks SQLite integrity, the exact graph hash,
every pool row, every loop's closure, every referenced graph edge and the
repetition limit before writing an archive. Complete-area packages also
require all build shards and surfaces. The sample package was extracted and
ran its own API, serving three closed pool routes with GPX; private/auth
files were absent. It contains a `start.sh`, pinned requirements, matching
graph/pools, attribution, source metadata and the recorded fallback.

`finalize_pool_build.py` watches the ongoing build, keeps the Mac awake
while waiting, retries a finished failed build once at lower concurrency,
and audits/packages the complete artifact before invoking an optional
upload callback. Its progress is recorded in
`output/presentation/completion-status.json`. No generated data is committed
to the repository.
