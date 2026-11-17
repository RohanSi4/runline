# Deployed validation: runline-nine.vercel.app

Measured on November 16, 2026 (evening Eastern, 01:46 to 02:00 UTC on November
17) against the public app, for the QA / Validation Report. Results are
graded against the proposal's wording. Where a criterion has two clauses, each
is graded, and the criterion passes only if both hold.

## What was tested

| Item | Value |
|---|---|
| Public URL | https://runline-nine.vercel.app (its `/api/*` rewrites to runline-capstone.vercel.app, which forwards coverage misses to the Oracle regional backend) |
| Backend `/api/version` | `ebcd150623c887488450b5e90c7cc9ce759d49a9` (PR #13), behind `main` (`f482881`) |
| Regional backend `/api/version` | `unknown` (`RUNLINE_BUILD_SHA` is not set there) |
| Frontend | deployed `/` and `/assets/app.js` are byte-identical to `web/index.html` and `web/app.js` at `f482881` |
| `/api/areas`, loop coverage | Charlottesville VA, Los Angeles CA, New York NY, Atlanta GA, Boston MA, Phoenix AZ, Washington DC |
| `/api/capabilities`, point to point | Charlottesville VA only |
| `/api/health` | `{"status": "ok"}` |

The backend commit predates `/api/warm` on the capstone API, the waypoint
rescale cap (`1cf0466`) and the warm-up changes, so this run validates
`ebcd150`'s routing, not `main`'s. `scoring.py` is identical in both.

## Method

`scripts/validate_deployed.py` sends strictly sequential requests with a
1 s pause and stops on three consecutive 5xx answers. It writes every request,
captured response and result to the git-ignored `output/deployed-validation/`,
with `summary.md` as the generated table. `scripts/ui_display_check.mjs` feeds
those captured responses through the deployed `app.js` in a Node VM, using a
DOM shim in the style of `tests/web_flow.mjs`. In total about 208 requests
went to production: 178 from the script (177 to runline-nine, 1 to the
regional `/api/version`) and about 30 from the team's own check scripts. No
5xx or transport errors occurred.

Request bodies are the ones `web/app.js` sends. Starts are public landmarks.
The ten Charlottesville starts are chosen by name from the deployed
`/api/places` catalog: The Lawn, McIntire Park, Pen Park, Washington Park,
Court Square Park, IX Art Park, Riverview Park, Greenleaf Park, Forest Hills
Park and Rives Park. Metro starts are typed addresses that the API geocodes,
as the page does outside the catalog:

| Area | Starts |
|---|---|
| Washington, DC | Washington Monument, Dupont Circle, Lincoln Park |
| New York | New York City Hall, Bryant Park, Prospect Park |
| Los Angeles | Los Angeles City Hall, MacArthur Park, Echo Park Lake |
| Atlanta | Piedmont Park, Atlanta City Hall, Grant Park |
| Boston | Boston Common, Boston City Hall, Cambridge City Hall |
| Phoenix | Phoenix City Hall, Encanto Park, Tempe Beach Park |

Sequences follow the page:

- **Charlottesville:** choosing a suggestion calls `/api/warm`, then requests
  go 3, 5, 3, 5 mi. The first request per start counts as cold.
- **Metros:** a typed-address 5 mi request (cold, and it includes geocoding),
  then the `/api/warm` call that `renderRoutes()` triggers, then 3, 8 and 5 mi
  by coordinates.

All requests are mixed surface, balanced elevation, three results. No
coordinates appear in this file or the scripts.

## Results

| Criterion | Result | Numbers |
|---|---|---|
| US10 response time | **PASS** | Warm 3 and 5 mi: n=66, median 0.48 s, p95 1.95 s, max 14.04 s. Every area is within median < 10 s and p95 < 20 s (table below). |
| US11 additional area | **PASS** | All 6 metros passed every request: 72/72 requests, 216/216 routes closed, GPX parses and matches geometry point for point, all routes within +/-0.25 mi. |
| US9 drive radius | **FAIL** | (a) Radius **PASS**: 10/10 cases answered, 19 alternative-start routes, 0 outside the radius. (b) Display **FAIL**: the UI shows neither the route score nor the driving cost. |
| US4 displayed metrics | **FAIL** | (a) Values match **PASS**: 0 mismatches in 4,284 checks over 378 cards from 126 real responses. (b) Components displayed **FAIL**: no distance error, crossings, repeated %, shape or score breakdown appears anywhere. |
| US8 (supporting only) | **PASS (automated)** | 5/5 autocomplete names, map-click body, typed address and map click give identical routes (2/2), 35/35 node tests. The proposal's three-person manual browser checklist is still required. |
| Error handling | **PASS** | 8/8 bad requests returned 422 with an explained `detail`, and none returned 5xx. |
| Point to point | **PASS** | The Lawn to McIntire Park: one open path of 3.48 mi, live, 1.66 s, endpoints equal origin and destination, GPX matches. |
| `presentation_check.py --require-pool` (runline-nine) | **FAIL** | Stops at the autocomplete check: `/api/places` arrives Brotli-encoded (see Findings). |
| `presentation_check.py --require-pool` (runline-capstone) | **PASS** | Expected commit, 10/10 cases, 30 closed in-band routes, 9 pool cases and 1 live drive case, both error checks. |
| `check_direct_routes.py` (runline-nine) | **FAIL** | Same `/api/places` decode error, before any route request. |

### US10: end-to-end wall time from one residential connection

p95 is nearest-rank. With n=6 per metro, p95 equals the maximum.

| Area | Sample | n | Median s | p95 s | Max s | Pool / live |
|---|---|---:|---:|---:|---:|---|
| Charlottesville | warm 3+5 mi | 30 | 0.46 | 0.77 | 1.39 | 29 / 1 |
| Charlottesville | cold first | 10 | 0.45 | 3.90 | 3.90 | 9 / 1 |
| Washington, DC | warm 3+5 mi | 6 | 0.45 | 6.09 | 6.09 | 6 / 0 |
| Washington, DC | cold (typed) | 3 | 6.81 | 7.01 | 7.01 | 3 / 0 |
| New York | warm 3+5 mi | 6 | 0.89 | 2.02 | 2.02 | 2 / 4 |
| New York | cold (typed) | 3 | 18.30 | 20.28 | 20.28 | 1 / 2 |
| Los Angeles | warm 3+5 mi | 6 | 0.61 | 14.04 | 14.04 | 6 / 0 |
| Los Angeles | cold (typed) | 3 | 6.86 | 7.02 | 7.02 | 3 / 0 |
| Atlanta | warm 3+5 mi | 6 | 0.33 | 0.74 | 0.74 | 6 / 0 |
| Atlanta | cold (typed) | 3 | 4.47 | 5.02 | 5.02 | 3 / 0 |
| Boston | warm 3+5 mi | 6 | 0.51 | 0.60 | 0.60 | 6 / 0 |
| Boston | cold (typed) | 3 | 5.57 | 23.09 | 23.09 | 3 / 0 |
| Phoenix | warm 3+5 mi | 6 | 0.36 | 0.59 | 0.59 | 6 / 0 |
| Phoenix | cold (typed) | 3 | 7.02 | 7.08 | 7.08 | 3 / 0 |

8 mi requests (18 in metros) had a median under 1 s. New York's two live
8 mi requests took 8.6 and 9.7 s. Two cold typed-address requests exceeded
20 s: Cambridge City Hall (23.1 s) and Bryant Park (20.3 s). US10 is stated
for warm requests, so these do not fail it, but a first search in a new
metro tile can take 5 to 23 s. The two slow warm outliers were Echo Park Lake
3 mi (14.0 s) and Lincoln Park 5 mi (6.1 s), both pool answers, so the delay
is likely a tile reload on the regional backend. Drive-radius requests always
plan live and took a median of 15.2 s, with a maximum of 24.0 s (US9 below).
If they were counted under US10, p95 would exceed 20 s.

The Charlottesville `/api/warm` call goes to the Oracle backend, not the
capstone API that answers Charlottesville routes, as `docs/unified-deployment.md`
says. Charlottesville cold requests were still fast: 8 of 10 under 1 s. The
exceptions were The Lawn (3.4 s) and McIntire Park (3.9 s, live). The main session also saw a browser 5 mi request from the
Rotunda take 10 to 14 s while this run was active. That is anecdotal; the
scripted typed-address Rotunda request took 1.27 s.

### US11: route validity per area

| Area | Requests | Closed | GPX matches | All in +/-0.25 mi | Within 5% / 10% | Pool / live |
|---|---:|---:|---:|---:|---:|---|
| Charlottesville (primary) | 40 | 120/120 | 120/120 | 120/120 | 100 / 120 | 38 / 2 |
| Washington, DC | 12 | 36/36 | 36/36 | 36/36 | 36 / 36 | 12 / 0 |
| New York | 12 | 36/36 | 36/36 | 36/36 | 35 / 36 | 4 / 8 |
| Los Angeles | 12 | 36/36 | 36/36 | 36/36 | 36 / 36 | 12 / 0 |
| Atlanta | 12 | 36/36 | 36/36 | 36/36 | 34 / 36 | 12 / 0 |
| Boston | 12 | 36/36 | 36/36 | 36/36 | 35 / 36 | 12 / 0 |
| Phoenix | 12 | 36/36 | 36/36 | 36/36 | 34 / 36 | 12 / 0 |

Across all 126 loop responses (378 routes):

- Mean absolute distance error was 0.087 mi; the largest was 0.241 mi.
- Each route's geometry length matched its reported distance to under 0.001%.
- The score was recomputed from the returned metrics with `scoring.py`'s
  formula for every component except elevation, which is normalized over
  candidates the response omits and so was only range-checked. There were
  0 mismatches, and the returned order matched the ranking key in every
  response.

New York answered 8 of 12 requests by live planning; every other metro was
all pool.

### US9: drive radius

The radius is first applied in straight-line miles, then as road distance on
the shipped Charlottesville drive graph (`starts._starts_from_precomputed`).
`drive_distance_miles` is that road distance, and it is what the
`driving` score component uses (0.35 per mile). Both distances were checked
against the radius.

| Start | Radius mi | Distance mi | Time s | Alt-start routes | Drive mi (straight-line mi) |
|---|---:|---:|---:|---:|---|
| Jordan Park | 1 | 3 | 10.4 | 2 | 0.48 (0.27), 0.48 (0.27) |
| Meade Park | 3 | 3 | 11.8 | 2 | 0.28 (0.31), 0.37 (0.40) |
| Quarry Park | 5 | 3 | 14.3 | 2 | 0.48 (0.33), 0.48 (0.33) |
| Jenkins Park | 1 | 5 | 18.6 | 2 | 0.14 (0.16), 0.21 (0.18) |
| Azalea Park | 3 | 5 | 16.1 | 2 | 0.64 (0.56), 0.56 (0.23) |
| Fifeville Park | 5 | 5 | 23.6 | 3 | 0.18 (0.17), 0.18 (0.17), 0.30 (0.27) |
| Starr Hill Park | 1 | 3 | 12.0 | 1 | 0.28 (0.19) |
| Longwood Park | 3 | 8 | 24.0 | 2 | 0.25 (0.30), 0.46 (0.37) |
| Greenbrier Park | 5 | 3 | 14.7 | 2 | 1.31 (0.86), 1.47 (0.85) |
| Northeast Park | 1 | 5 | 15.7 | 1 | 0.40 (0.56) |

Every case reported 2 discovered starts (`start_candidate_limit=2`). The
response only includes starts that produced a returned route, so starts that
were discovered but lost the ranking cannot be checked. For 5 of 19, the
reported drive is shorter than the straight line (Northeast Park: 0.40 vs
0.56 mi). That happens because road distance is measured between the
nearest drive-graph nodes, so the legs from the start point to the road
network are not counted. Near a radius boundary this could admit a start
whose real drive exceeds the radius; no case here was near one.

The UI shows the alternative start's name on the card and detail panel
(19/19), and "2 nearby starts checked" in the header (10/10). It never shows
the route score or the drive distance or driving cost. `web/app.js` does
not read `drive_distance_miles`, `score` or `score_components`, so clause
(b) fails. The command-line `preview.html` does show "x.x mi drive".

### US4: displayed values and components

(a) **PASS.** Every value the cards, aria-labels and detail panel show
matches the route JSON within display rounding:

- distance to 2 dp
- signals as an integer count
- climbing to the nearest foot, or a dash below 95% elevation coverage
  (all 378 captured loop routes had coverage, so the dash path was not exercised)
- trail percent to the nearest whole number
- the start label

The header target distance and the "nearby starts checked" text also match.

(b) **FAIL.** Components the scorer uses, against what the UI displays
anywhere (cards, detail panel, aria-labels, titles and the deployed static
HTML, including hidden elements):

| Scorer component (`rank_candidates`) | Displayed |
|---|---|
| distance | Partly: actual and target distance are shown, but not the error |
| traffic_signals | Yes, count |
| major_crossings | No |
| surface | Partly: trail % |
| elevation | Partly: climbing ft |
| repeated_segments | No |
| shape | No |
| driving | No, only the start label |
| total `score` | No |

History: the web UI has never shown `score`, `score_components`, a distance
error or drive distance. `aab8410` (November 11, "Improve route comparison,
map controls, and request recovery") added a `route-insight` line, "N mapped
signals · N major crossings · N% repeated". `ebcd150` (PR #13, "Remove
planner copy and simplify map overlays", the same day, and the commit the
backend runs) removed it, along with the `route-insight` element, a `route-source`
label, the surface composition panel and that panel's test.

## Findings to act on (no production code was changed)

1. **`/api/places` is served Brotli-encoded to clients that do not accept
   it.** Through runline-nine, the edge-cached response (`x-vercel-cache:
   HIT`, `cache-control: public, max-age=86400, immutable`, no `Vary`) has
   `content-encoding: br` even when the request sends `Accept-Encoding:
   gzip` or `identity`. Browsers decode Brotli, so the page works. Python
   `requests` without a Brotli package cannot decode it, and that is why
   both `presentation_check.py` and `check_direct_routes.py` fail against
   runline-nine. Against runline-capstone directly, `presentation_check.py`
   passes. To reproduce:
   `curl -s -D - -o /dev/null -H 'Accept-Encoding: gzip' https://runline-nine.vercel.app/api/places`.
2. **US4 and US9 display gaps** (above). The data is in every response, but
   the cards do not render it.
3. **Live fallback can return only awkward loops.** McIntire Park 3 mi
   (live, both repetitions) returned three in-band routes that are all
   slivers (compactness 0.13), and two repeat 23% of their length. That is
   above the scorer's 10% `MAX_REPEATED`, which only orders routes; it does
   not exclude them. Repeated % is not shown, so a runner gets no warning.
   New York City Hall 8 mi route 3 also had compactness 0.175.
4. **The backend lags `main`.** The capstone API runs `ebcd150`, and the
   regional backend reports no commit.
5. **Cold metro searches and drive-radius searches are slow** (5 to 23 s and
   10 to 24 s; see US10).

The request bodies for every case, including the failures above, are in
`output/deployed-validation/requests.jsonl` and `captures/`, which are not
tracked.

## Limitations

- This is one run from one machine and network. Serverless instance
  warmth and the regional tile cache were not controlled. Metro samples are
  small (six warm requests per metro), so their p95 is their maximum.
- Metro cold times include server-side Nominatim geocoding, since that is
  the page's path for a typed address.
- The UI harness is a DOM shim running the real deployed `app.js` on real
  responses. It checks text and attributes, not layout or visibility, and it
  is not a visual browser test.
- US8 here is supporting evidence. The proposal's manual browser checklist
  by three team members is not replaced by it.
- Only alternative starts that produced a returned route are visible to the
  radius check. The elevation score component is only range-checked.

## Reproduce

```bash
PYTHONPATH=$PWD/src .venv/bin/python scripts/validate_deployed.py           # about 210 requests
PYTHONPATH=$PWD/src .venv/bin/python scripts/validate_deployed.py --summarize   # offline, rebuilds summary.md
node scripts/ui_display_check.mjs output/deployed-validation               # offline UI checks only
```

`--phases` selects a subset: meta, cville, metros, errors, us8, drive, p2p,
presentation, direct and ui. The request budget (default 250) counts across
invocations.
