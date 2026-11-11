# One public app for Charlottesville and DC

The canonical site is https://runline-nine.vercel.app. Its Vercel API rewrite
points to https://runline-capstone.vercel.app, which hosts the matching rebuilt
Charlottesville graph and complete pool file. Both URLs expose the same API.

The combined backend sets `RUNLINE_REMOTE_API=https://129-153-47-234.sslip.io`.
Local planning runs first. Only a local coverage error forwards the resolved
origin and preferences to that direct regional backend. Addresses are resolved
once; route geometry and GPX come back together from the regional service.
Its coverage labels are combined with the local labels. A remote outage leaves
local routing available and returns an explained 503 for regional requests.

Do not set `RUNLINE_REMOTE_API` to either public app URL: that would forward
back to the combined API. The Oracle service should leave it unset and retain
its existing indexed DC graphs and pools. No Oracle files were modified.

To reproduce the combined artifact, use `scripts/stage_presentation.py` with
the audited rebuilt graph, matching complete pool, capstone project config,
recorded HTML and `--remote-api https://129-153-47-234.sslip.io`. The matching
data is in the capstone draft release and local ignored presentation output;
it is intentionally absent from Git. Stage only from committed source.

Deploy with `--prod --skip-domain`, set `RUNLINE_BUILD_SHA` to the staged source
commit, verify the protected candidate's version, both coverage labels and real
routes in both regions, then promote it. The original site's Git deployment
only changes its API rewrite and frontend; it does not need the large artifacts.

Keep the old immutable deployment available for rollback. The earlier e27b284
artifact is a standalone Charlottesville snapshot; it remains useful offline
and is not the combined deployment.
