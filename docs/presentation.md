# Capstone presentation — November 11, 1 pm Eastern

## Current unified app

Use https://runline-nine.vercel.app for both rebuilt Charlottesville and DC.
Both public URLs use the combined backend; no tab switch is required for DC.
See [presentation-script.md](presentation-script.md) for the five-minute script
and [unified-deployment.md](unified-deployment.md) for the hosting arrangement.
The pinned snapshot and rehearsals below document the earlier standalone
Charlottesville release.

## Links

- Public presentation build: https://runline-capstone.vercel.app
- Recorded fallback: https://runline-capstone.vercel.app/assets/saved-demo.html
- Existing Oracle-backed app: https://runline-nine.vercel.app
- Presentation source: https://github.com/RohanSi4/runline/pull/6

The presentation deployment is pinned to application commit
`e27b2849452a465074dfb6a3d64baee636d21caf`, graph hash prefix
`66b524897627167e`, and the complete 11,417-pool dataset. Its public alias is independent
of the original app's Oracle proxy.

## Demo order

1. Open the pinned presentation URL recorded in the release report. Keep
   `output/presentation/offline-demo-new.html` available locally as a fallback.
   The saved example is explicitly labeled as recorded.
2. Keep the prefilled UVA start, choose **5 miles**, **Mixed**, **Balanced**,
   and **Here**, then generate routes. Compare their distances and signals,
   select another route on the map, and download GPX.
3. Use the quick **3**, **8**, or **12** mile buttons to show distance changes.
   These four default-start cases have been rehearsed against the rebuilt API.
4. Change surface or hills to explain how preferences influence cost/ranking.
   If no saved loops qualify, the request plans live on the same graph.
5. If time permits, show **Better starts → 1 mi**. It compares additional
   starts and takes longer than a saved-pool request.

6. Optional DC example: open the original app, enter the Washington Monument
   with Washington, DC in the address, and request five mixed-surface miles.
   A live rehearsal returned three closed in-band pool routes with matching
   GPX tracks. This is Krish's Oracle deployment of the earlier DC pilot.

If the network or API fails, open the standalone HTML fallback. It embeds
actual captured routes, streets and GPX tracks and makes no network requests.
It supports selecting routes and exporting GPX; it does not generate new
routes. Explain that distinction when using it.

## Accurate talking points

- The objective is a loop near the requested distance with fewer interruptions,
  subject to mapped streets, paths and terrain. It is not a guarantee that a
  route is signal-free, accessible or globally optimal.
- The planner searches a walkable OSM graph, removes simple backtracking,
  measures each loop and ranks it by distance, signals, crossings, repetition,
  shape, surface and hills. GPX is an export for compatible navigation tools;
  automatic watch synchronization is not implemented.
- Pools shift search work ahead of the request. A runner can join a saved loop
  near where it passes, and cells share nearby candidates. Unavailable pools
  fall back to live planning.
- Metro cells identify their owning tile. The API loads that tile's exact graph,
  validates its hash, and keeps a bounded cache. This serving path is tested;
  nationwide coverage has not been computed or deployed.
- Charlottesville is the demonstrated coverage. The original live app uses
  Vercel's frontend and Krish's Oracle API. The separate presentation build
  runs the new API and rebuilt graph directly, with the complete rebuilt Charlottesville pool file. Requests with no
  qualifying saved loops still plan live.

## Evidence and limits

- Integration PR #5 is merged. Presentation tooling is in PR #6.
- The ten-scenario production rehearsal returned 30 closed, in-band routes;
  every GPX track matched its displayed line. The rebuilt local API passed
  the same rehearsal, including errors and autocomplete.
- The rebuilt graph has 40,422 nodes, 569 signal nodes including 301 signaled
  crosswalks, and 100% elevation coverage. It uses the cached OSM source data;
  this is a corrected build, not a claim that OSM was freshly surveyed.
- On 32 live-routing benchmark cases, distance fit improved from 91 to 93 of
  96 returned routes after rebuilding the graph. The geometric crossing audit
  reported 97.4% precision and 100% recall.
- Shape statistics are mixed: slivers below 0.20 decreased from nine to seven,
  but short revisits rose from zero to three. Do not claim perfect loops.
- The earlier **56.5% → 96%** pool comparison used the original shipped graph,
  the same 40 starts and old/regenerated sample pools. It is a measured sample
  result, not a nationwide or production hit-rate guarantee. The rebuilt
  graph's sample is measured separately in the new release report.
- OSM tags can be incomplete; elevation comes from a 90 m terrain model.

## Rehearsal commands

Run from this checkout using its virtual environment, or substitute the
existing RunLine environment's Python path:

```bash
python scripts/presentation_check.py --api <presentation-url> --require-pool --expected-commit <release-commit> --capture --output output/presentation/final-check.json
python scripts/offline_demo.py --capture output/presentation/responses/default-5.json --graph graphs/charlottesville-va.pkl.gz --output output/presentation/offline-demo.html
```

The release must pass its API/GPX check on the pinned URL. A successful Vercel
build alone does not establish that the right backend is running. The
`/api/version` endpoint identifies it separately from the frontend.

## Work still outside the demo

The complete Charlottesville build finished all 96 shards with no failures,
producing 11,417 pools and 605,206 loops in 3.54 hours. Every stored loop was
audited against the matching graph before packaging. The complete dataset is
now deployed at the public capstone URL. Its final ten-scenario rehearsal
passed: 30 closed in-band routes, matching GPX tracks, nine pooled scenarios
and one live nearby-start scenario.

The 600-request check (40 starts × five distances × three surfaces) answered
548 requests from pools (91.3%): mixed 184/200, road 181/200, trail 183/200.
Twenty hits returned two qualifying routes. Other requests fell back to live
planning. Local median pooled latency was 64 ms; public warm pooled requests
in the rehearsal took about 0.4–0.9 s, with a 2.7 s first request.

The complete and sample packages are attached to the capstone draft release.
The latest complete snapshot matches the final presentation application;
earlier snapshots are retained as backups. Keep the exact graph and SQLite
file together. DC is now verified live on the original app. Its artifacts
and Oracle SSH access remain unavailable locally. No Rivanna allocation was verified or jobs submitted in this session. The
team reports batch `b1` (New York and Los Angeles) running on the UVA CS
cluster. Full top-50 coverage remains incomplete and is not part of the demo.
