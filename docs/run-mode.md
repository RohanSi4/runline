# Run on your phone and choose separate endpoints

Use https://runline-nine.vercel.app. Generate a route, select its card, and tap
**Run this route**. Allow location access. Recording begins with the first
usable GPS fix, showing recorded distance, active time, average pace, location
accuracy and a separate measured track over the planned route. GPX is optional.

Pause/resume manually, pan the map to stop following, or use Follow location.
Finish stops GPS/timers and shows a measured summary. Back to routes restores
the planner. Exit during recording asks before discarding the current session.

This is a foreground browser session. Keep the screen open; hiding the page
pauses recording and requires Resume. Screen wake lock is attempted where
supported but is not guaranteed. Poor fixes and missing segments are not filled
in with invented movement. Time starts with usable GPS and excludes locating
and manual pauses. GPS distance is an estimate, not the selected route length.

The track remains in this page's memory, is not uploaded, and is not saved after
leaving/reloading. This release does not provide account history, background
recording, turn instructions or automatic watch synchronization. On a phone,
use the browser's Add to Home Screen option for an app shortcut; the manifest
requests a standalone display where supported.

The foreground constraints follow the [Geolocation specification](https://www.w3.org/TR/geolocation/).
Manifest display preferences are documented by [MDN](https://developer.mozilla.org/en-US/docs/Web/Progressive_web_apps/Manifest/Reference/display).

## Start → finish

Choose **Start → finish**, enter or select both locations, or use the A/B map
buttons to set each endpoint. Surface preference applies; the route goes
directly between the endpoints using running-specific weighted path search.
Distance-target, hill and drive controls belong to loop mode and are hidden.

Direct routes run wherever this server holds the map locally: the shipped
**Charlottesville** graph, and any metro whose tile graphs are present under
`RUNLINE_METROS_DIR/<run>/tile-graphs`. Both endpoints must fit one map and
be within 300 m of a mapped walkable node. Pins move to the actual mapped
endpoints; meaningful snapping is reported. Identical, unreachable or
unsupported endpoints return an explained error.

On a metro, the start's cell names its owning tile, as for loops. If that
tile's disc also holds the finish, the path is searched on its graph. If not,
other tiles whose discs hold both endpoints are tried (tiles overlap, and the
pool index records each tile's centre and radius), deepest inside first. A
request loads at most two tile graphs through the same bounded tile cache
loops use. Paths are not stitched across tiles: endpoints with no common tile
get an explained error asking for a closer finish. A shipped area holding
both endpoints keeps priority over metro tiles.

`/api/capabilities` reports separate mode coverage. `point_to_point` lists
the shipped areas and only those metros whose every tile graph with streets
is present on this server; a metro with pools but missing tile graphs is
listed for loops only.

Loop requests outside local coverage continue through the regional Oracle
service. A direct request is forwarded there only if that backend's own
`/api/capabilities` lists `point_to_point` coverage. The check uses a 2 s
connect and 3 s read timeout and is cached for five minutes (a failed check
for 30 s). An older backend without that report could ignore destination
fields and return a loop, so it never receives direct requests, and a
forwarded answer that is not an open start to finish path is rejected with
a 503. The regional backend's direct areas are added to the combined
`/api/capabilities` report.

## Verification

Run `python -m pytest -q`, `node --test tests/web_flow.mjs tests/run_session.mjs`,
the existing `scripts/presentation_check.py`, and
`scripts/check_direct_routes.py --api <url> --output output/direct-check.json`.
The direct rehearsal verifies open paths and point-for-point GPX, plus same,
missing and unsupported endpoint errors. GPS tests use synthetic samples and
exercise real session logic; they do not replace an outdoor phone trial.
