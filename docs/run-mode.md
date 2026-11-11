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

The initial direct-route coverage is **Charlottesville**. Both endpoints must
fit the same supported map and be within 300 m of a mapped walkable node.
Pins move to the actual mapped endpoints; meaningful snapping is reported.
Identical, unreachable or unsupported endpoints return an explained error.

DC loop generation continues through the existing Oracle service. New direct
requests never fall back to that older API, which could ignore destination
fields and return a loop. `/api/capabilities` reports separate mode coverage.

## Verification

Run `python -m pytest -q`, `node --test tests/web_flow.mjs tests/run_session.mjs`,
the existing `scripts/presentation_check.py`, and
`scripts/check_direct_routes.py --api <url> --output output/direct-check.json`.
The direct rehearsal verifies open paths and point-for-point GPX, plus same,
missing and unsupported endpoint errors. GPS tests use synthetic samples and
exercise real session logic; they do not replace an outdoor phone trial.
