# Presentation release validation

The integration from PR #5 is merged. PR #6 adds reproducible replacement
artifacts, release verification, an offline saved example, locally served
Leaflet, and support for returning two qualifying pool routes on the default
up-to-three request. Other result counts retain their existing behavior.

The full suite passes **187 tests** on the rebuilt graph with its matching
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


## Completed area build and public rollout — November 11

All **96/96 shards** completed with no failures in **12,728 seconds (3.54 h)**.
The complete file contains **11,417 pools and 605,206 loops**, all three
surfaces, and is **95.0 MiB**. Its SHA-256 is
`c18a02a032cc1e4e825bea010cddc840863ec9a386f1ae4342de14801024f03c`.
Every stored loop passed the exact-graph edge, closure and repetition audit.
The complete archive and checksum report are attached to the draft release.
The completion supervisor finished successfully and no retry was required.

The complete-data Vercel build fit within the platform's current limits.
Candidate road-surface routes and the backend revision were verified before
promotion. The public capstone URL now points to deployment
`dpl_4Wr3A7vyhR9Pw8sUuiyPkihU62aq`. Runtime application files are unchanged
from the pinned `77f6c3e` release; later commits contain tooling and notes.
The original Oracle-backed domain was preserved.

The final unauthenticated public rehearsal passed all ten scenarios: **30
closed, in-band routes with matching GPX tracks**, nine pooled scenarios and
one live nearby-start scenario. The backend revision, health, coverage,
autocomplete, local assets and both validation-error checks also passed.
Warm public pooled requests in this rehearsal took 425–850 ms; the first
request took 2,679 ms and the nearby-start request 9,012 ms. These are single
rehearsal observations, not load-test guarantees.

A separate local 600-request pool check used all three surfaces:

| Surface | Pool hits / requests |
|---|---:|
| Mixed | 184 / 200 (92.0%) |
| Road | 181 / 200 (90.5%) |
| Trail | 183 / 200 (91.5%) |
| All | 548 / 600 (91.3%) |

Twenty hits returned two qualifying routes. Median pooled time was 64 ms,
p95 129 ms; the other 52 requests require live planning. This check skipped
live timing and should not be combined with the historical paired study as
if they were the same experiment.


## Final UI snapshot and DC verification

The final presentation application is pinned to
`e27b2849452a465074dfb6a3d64baee636d21caf`, deployment
`dpl_HQpjQaho6s2F8XLgto256ywE2Vkh`. The data hashes and complete-area pool
counts above are unchanged. Its client script was fetched from the candidate
and matched the tested source byte-for-byte before promotion.

Address results now move the map's start pin to the resolved origin while
preserving the typed label. A late response cannot overwrite a newer start
selection. Three controller regressions cover resolved pins, normal replies
and late replies; the full Python suite now passes 187 checks, including the
client-state check. Browser rendering remained unavailable, so these are
controller/API checks rather than a claimed visual browser test.

The final unauthenticated ten-scenario public rehearsal again returned 30
closed in-band routes with matching GPX, with nine pooled cases and one live
nearby-start case. An additional full-address lookup returned three closed
in-band pool routes. Separately, the original app's coverage endpoint now
lists Charlottesville and Washington, DC. A public DC landmark request
returned three closed in-band pool routes, with matching GPX, at
5.104/4.931/5.061 miles. This verifies Krish's newly deployed DC pilot; its
artifacts were not downloaded or rebuilt in this session.

The Oracle service was not modified in this session. The team subsequently
recorded its successful move to main and the indexed DC run layout in
`handoff.md`. That update is preserved alongside this release. The team also
reports batch `b1` (New York and Los Angeles) running on the UVA CS cluster;
full top-50 coverage remains incomplete.


Final checks include the ordinary repository graph (187 passing checks), the
rebuilt graph with its own crossing fixture (187 passing checks), and three
client-controller regression cases. No application code changed after the
final `e27b284` snapshot; remaining edits are tests, notes and packaging.
