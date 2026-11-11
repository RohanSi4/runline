# One public app for Charlottesville and DC

The canonical site is https://runline-nine.vercel.app. Its Vercel API rewrite
points to https://runline-capstone.vercel.app, which hosts the matching rebuilt
Charlottesville graph and complete pool file. Both URLs expose the same API.

```
browser -> runline-nine.vercel.app (page; vercel.json rewrites /api/*)
        -> runline-capstone.vercel.app (FastAPI, rebuilt Charlottesville graph + complete pool)
        -> on a coverage miss only, RUNLINE_REMOTE_API -> Oracle VM (DC tile graphs + pools)
```

The combined backend sets `RUNLINE_REMOTE_API=https://129-153-47-234.sslip.io`.
Local planning runs first. Only a local coverage error forwards the resolved
origin and preferences to that direct regional backend. Addresses are resolved
once; route geometry and GPX come back together from the regional service.
Its coverage labels are combined with the local labels. A remote outage leaves
local routing available and returns an explained 503 for regional requests.

Do not set `RUNLINE_REMOTE_API` to either public app URL: that would forward
back to the combined API. The Oracle service should leave it unset and retain
its existing indexed DC graphs and pools. Oracle operations are in
[handoff.md](handoff.md), task 4.

## Releasing runline-capstone

`scripts/release_capstone.py` is the single entry point. Anyone with access to
the Vercel team and the GitHub release can run it; nothing depends on one
person's local output folders.

What is pinned, in `deploy/capstone-artifacts.json`:

| Pin | Value |
|---|---|
| Release | `capstone-2026-11-11`, asset `runline-complete-e27b284.tar.gz` (sha256 `4b0a552e...`) |
| Pool | `pools/charlottesville-va.sqlite`, 95.0 MiB, sha256 `c18a02a0...` (11,417 pools, 605,206 loops) |
| Graph | `graphs/charlottesville-va.pkl.gz`, sha256 `66b52489...`, plus drive graph, starts and places |
| Recorded fallback | `saved-demo.html`, served as `/assets/saved-demo.html` |
| Regional API | `https://129-153-47-234.sslip.io` |
| Vercel project | `runline-capstone` (org and project IDs, so no local `.vercel/` link is needed) |

`graphs/manifest.json` is checked by a hash of its parsed JSON, so formatting
differences between the archive and a staged copy do not matter.

### Procedure

1. Start from a clean checkout of the commit to release, pushed to GitHub:

   ```bash
   git fetch origin && git checkout --detach <sha>
   git status --short        # must print nothing
   ```

   The script refuses a tree with modified, staged or untracked files, and
   `--deploy` refuses a commit that is on no remote branch. It stages
   `git archive HEAD`, never the working copy. It also refuses if
   `osm.py`, `scoring.py`, `models.py` or `precompute_pools.py` changed since
   the pool build commit (`9b56ec5`); that needs new pools, not a redeploy.

2. Stage and check:

   ```bash
   .venv/bin/python scripts/release_capstone.py                  # downloads the release
   .venv/bin/python scripts/release_capstone.py --artifacts-dir DIR   # or a local copy
   ```

   The release is a draft, which GitHub shows only to accounts with write
   access to the repository. If `gh release download` reports "release not
   found", run `gh auth switch` to such an account or pass `--artifacts-dir`.
   A local copy may be an extracted release archive or an earlier staging
   directory (`graphs/`, `pools/`, and `saved-demo.html` or
   `web/saved-demo.html`).

   The script then:

   - verifies the archive and every pinned file against the manifest, listing
     every mismatch before refusing;
   - stages `output/release/<sha12>/stage/` with `stage_presentation.stage`:
     source from `git archive`, the graph files, the pool, the recorded
     fallback, `.vercel/project.json`, and a `vercel.json` whose `env` sets
     `RUNLINE_REMOTE_API` and `RUNLINE_BUILD_SHA=<sha>`;
   - serves that directory with uvicorn on a free localhost port
     (`RUNLINE_ALLOW_OSM_DOWNLOAD=0`) and runs `presentation_check.py
     --require-pool --expected-commit <sha>` and `check_direct_routes.py`
     against it. Reports land in `output/release/<sha12>/checks/`.
     By default the local check does not forward to Oracle and expects only
     Charlottesville coverage; `--check-remote` enables forwarding and then
     requires both coverage labels.
   - prints the deploy, candidate check, promote and public rehearsal
     commands. It does not run them.

3. Before promoting, record what is serving now, for rollback:

   ```bash
   curl -s https://runline-capstone.vercel.app/api/version
   vercel inspect https://runline-capstone.vercel.app      # note the deployment ID
   ```

4. Run the printed commands in order:

   ```bash
   vercel deploy --prod --skip-domain --yes --cwd output/release/<sha12>/stage
   export CANDIDATE_URL=<URL printed by vercel deploy>
   vercel curl "$CANDIDATE_URL"/api/version --cwd output/release/<sha12>/stage
   vercel promote "$CANDIDATE_URL" --yes --cwd output/release/<sha12>/stage
   python scripts/presentation_check.py --require-pool --expected-commit <sha> \
       --api https://runline-capstone.vercel.app --output output/release/<sha12>/public-check.json
   python scripts/presentation_check.py --require-pool --expected-commit <sha> \
       --api https://runline-nine.vercel.app --output output/release/<sha12>/site-check.json
   ```

   `--skip-domain` creates a production build without moving the public URL;
   the candidate is protected, so check it with `vercel curl`. Confirm it
   reports the staged commit and that its `/api/areas` lists both
   Charlottesville and Washington, DC before promoting. The last command
   proves the canonical site reaches the new backend through its rewrite.
   Use `--vercel "npx vercel@latest"` if the CLI is not installed globally;
   `--deploy` runs step one itself but still never promotes.

The original site's Git deployment only changes its API rewrite and frontend;
it does not need the large artifacts and is not released by this script.

### Changing the data

Build and audit new pools on the exact graph, package them with
`scripts/package_presentation.py`, and attach the archive to a release. Then,
in one commit, update `deploy/capstone-artifacts.json`: the tag, asset name
and sha256, and each file's sha256. Never commit the artifacts themselves.
Reverting that commit restores the previous pins.

### Durable storage

The artifacts currently live only in the draft release `capstone-2026-11-11`
and in ignored local folders. A draft can be deleted or edited, and only
repository writers can see it. Publish the release (or copy the archive to
other durable storage and update the manifest) so the pinned files outlive
any one checkout. The script verifies hashes, so the storage location can
change without weakening the check.

## Rollback

Every Vercel deployment is immutable, so rolling back is a promotion, not a
rebuild. Keep the deployment ID recorded in step 3.

- **Capstone backend (most cases).** Point the public URL back at the
  previous deployment, then re-run the public rehearsal with that
  deployment's commit:

  ```bash
  vercel rollback <previous-deployment-id-or-url> --cwd output/release/<sha12>/stage
  # or: vercel promote <previous-deployment-id-or-url> --yes
  vercel ls runline-capstone --prod     # find older production deployments
  ```

  Earlier known production deployments: `dpl_HQpjQaho6s2F8XLgto256ywE2Vkh`
  (the `e27b284` UI snapshot) and `dpl_4Wr3A7vyhR9Pw8sUuiyPkihU62aq` (first
  complete-pool release, `77f6c3e`). Both carry the same pinned data but
  predate the combined backend, so they may not forward DC; check
  `/api/areas` after promoting one. Prefer the deployment recorded in step 3.

- **Regional backend only.** An Oracle outage already degrades safely: DC
  requests get an explained 503 and Charlottesville keeps working. Oracle's own
  rollback (the previous systemd unit) is in [handoff.md](handoff.md), task 4.

- **Canonical site.** If the capstone project itself is unusable, revert the
  `/api/(.*)` rewrite in the repository's `vercel.json` to
  `https://129-153-47-234.sslip.io/api/$1` through a normal PR, or promote the
  previous runline-nine deployment. That restores the original Oracle-only
  chain: Oracle's older Charlottesville pool plus DC.

The earlier e27b284 archive is a standalone Charlottesville snapshot with a
`start.sh`; it remains useful offline and is not the combined deployment.
