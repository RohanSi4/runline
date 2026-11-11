# RunLine handoff

## Presentation preparation

Integration PR #5 is merged. Active presentation work is on
`codex/presentation-ready` in the `runline-presentation` checkout, tracked by
PR #6. See [presentation.md](presentation.md) for the demo sequence and
[presentation-release.md](presentation-release.md) for its measured results.
The rebuilt graph and complete 11,417-pool, 605,206-loop dataset are audited
and deployed to the capstone URL. All 96 shards finished without failures;
the complete package and audit are attached to the capstone draft release.
A separate public presentation deployment is available at
https://runline-capstone.vercel.app, with a hosted recorded fallback. The original Oracle
backend has not been updated in this session because SSH access is missing.

## Recovery status

Claude's November 10 work is integrated on `codex/resume-runline` in the
`runline-recovery` checkout. The original five branches and their worktrees
are preserved. The integration includes upstream `32a7876`, which hosts the existing API
on Oracle. This recovery has not changed the deployed backend.

| Work | Current state |
|---|---|
| Current hosting | Charlottesville pools are already on Krish's Oracle VM; Vercel proxies `/api`. A live five-mile pool request was checked during recovery. |
| Optional hosting tools | Provisioning, systemd, Caddy, area-pool upload, backup and smoke scripts integrated as a portable VM recipe. No Azure VM is needed for the existing deployment. |
| Metro serving | Cell-to-tile index, graph hash checks, bounded graph cache and live fallback integrated. Tested with cropped Charlottesville tile graphs; actual DC artifacts are unavailable locally. |
| Pool hit rate | Loops start where they pass nearest the runner; requests read their cell and eight neighbors and measure the entire drawn route. Metro reads are restricted to the loaded tile. |
| Routing fixes | Builders retain signalized crosswalk tags and nodes; nearby signal nodes count as one event; self-crossing loop area counts enclosed faces. Browser validation errors display as text. |
| Rivanna | Cluster configuration, dry-run and smoke support integrated. No jobs submitted. |
| Graphs and pools | Repository graph remains unchanged for compatibility. The rebuilt graph and complete 95 MB pool file are deployed on the separate capstone project; the original Oracle assets remain in place. |

See [validation.md](validation.md) for the before/after routing checks and
pool comparison. Local sample pools are benchmark fixtures, not a complete
Charlottesville or DC deployment.

## How the app fits together

FastAPI serves `web/` and the route API. `planner.plan_routes` tries pool
serving first and falls back to live planning. Manifest areas have priority
over metro runs. Production installs use `requirements.txt`; its exact pins
must match the graph build environment.

An area pool lives at `RUNLINE_POOLS_DIR/<area>.sqlite` (default `pools/`).
A metro run lives at `RUNLINE_METROS_DIR/<run>/`, containing `pools.sqlite`
and `tile-graphs/<tile>.pkl.gz`. The merged SQLite file's `cells` table maps
each start cell to its owning tile. Pool rows from neighboring cells are
usable only if they belong to that same tile graph. Missing or corrupt pools
fall back to live routing; missing tile graphs produce a coverage error.

`RUNLINE_TILE_GRAPHS_CACHE` defaults to two graphs per worker. The VM service
uses `/srv/runline/pools`, `/srv/runline/metros`, and `/var/cache/runline`.
Measure the actual metro graphs before sizing a VM; Charlottesville memory
measurements do not establish DC's memory needs.

## Remaining work, in order

### 1. Integration merged; finish the presentation release

PR #5 is merged. The recovery branch brought together `host-pools-vm`, `serve-metro-tiles`,
`pool-hit-rate`, `signal-tags-and-metrics`, and the already combined
`rivanna-slurm-on-metro`. Validation results belong with its PR. Do not push
to `main` directly.

### 2. Recover and archive the existing artifacts

Krish's Charlottesville file has 13,092 pools, about 110 MB. The DC pilot
has 72,237 cells with pools, about 20.25 million loops, and a 2.3 GB SQLite
file. Those are historical build figures, not locally verified artifacts.
Krish shared a Drive folder separately; the complete files are not in this
checkout. Keep the exact matching graphs and `plan.json.gz` with each pool.
The originals on the CS cluster's `/bigtemp` are volatile.

The old DC merge needs its index added using its exact plan:

```bash
.venv/bin/python scripts/merge_pools.py --plan plan.json.gz --add-index pools.sqlite
```

Leave at least another pool file's worth of disk for the rewrite. Old
artifacts can be served with their original graphs or retained as an archive;
they do not contain the newly preserved signal nodes. Do not mix them with
rebuilt graphs: graph hash validation rejects that combination.

### 3. Rebuild graphs and matching pools

The signal fix changes graph topology and hashes. Build graphs with the
build dependencies (`osmnx`, `osmium` for PBF inputs, and `rasterio` for
Copernicus elevation). Confirm the elevation bake is complete before
publishing. Recompute pools on those exact graphs using the pool-generation
fixes in this branch, which exclude unusable loops and choose origins inside
routable cells. Charlottesville's rebuild is complete on the capstone project. DC and the
top-50 rebuilds have not run.

### 4. Update the existing hosted backend

Krish already completed pool hosting in upstream PR #4. Vercel serves the
page and proxies `/api/*` to an Oracle VM at
`https://129-153-47-234.sslip.io`. A health check and an actual five-mile
request confirmed the deployed API works during this recovery. The proxy in
`vercel.json` is preserved.

The recorded server setup is Ubuntu 24.04 on `VM.Standard.A1.Flex` (ARM),
4 OCPUs, 24 GB RAM and 190 GB disk, in Krish's Oracle account. The existing
`runline.service` runs uvicorn on `127.0.0.1:8765` with downloads disabled,
behind Caddy. Only Charlottesville's pool file is hosted.

After a code merge, the recorded update procedure is to SSH into the VM,
run `git pull` in `~/runline`, then `sudo systemctl restart runline`.
Confirm the new commit and a request's `source` afterwards. A graph rebuild
also requires its matching pool file; an old pool will be ignored.

The optional [deploy/README.md](../deploy/README.md) describes a separate
portable VM recipe. Its paths (`/opt/runline/app` and `/srv/runline`) and
uvicorn port (8000) differ from the existing Oracle setup. The current
backend does not need to move to Azure or adopt those paths.

Recorded Oracle traps: the public IP is ephemeral, so recreating the VM
changes the hostname in `vercel.json`. Its Ubuntu image had an iptables
`REJECT` rule; allow rules had to precede it and be saved with
`netfilter-persistent save`. Krish upgraded the account to Pay As You Go to
obtain A1 capacity and configured a $1 monthly budget email. Check current
resource eligibility and subscription limits before any infrastructure
change; these are deployment records, not a fresh billing audit.

Before serving DC, measure its actual tile graphs:

```bash
.venv/bin/python scripts/measure_tile_graphs.py metros/washington-dc/tile-graphs --together 2
```

Vercel's existing rewrite keeps browser requests on one origin, so no CORS
change is needed for this deployment.

### 5. Run the metros on Rivanna

Follow [slurm/README.md](../slurm/README.md), starting with local dry runs,
then a small cluster smoke run. Confirm a faculty PI's allocation and its
available SUs, task limits, scratch purge policy and durable archive storage
before submitting the full run. No cluster login or allocation was checked
in this recovery.

The batch config now includes Washington with Baltimore in `b6` and has an
empty `done` list: the old pilot must be recomputed for signal-preserving
graphs. Do not reuse old tile graphs or shards in a new run directory:
existing outputs are skipped rather than rebuilt. Archive each completed
batch, its plan, shards and exact tile graphs before proceeding.

## Repo rules and operational traps

- One branch and PR per task; never push directly to `main`.
- Changes touching `osm.py`, `scoring.py` or `models.py` require tests,
  `scripts/bench.py`, `scripts/shape_audit.py`, and before/after PR numbers.
- Keep personal starts and addresses in `.private/`, never in tracked output.
- Keep `requirements.txt` pins exact; rebuild graphs after networkx or
  shapely version changes.
- Do not use the CS cluster's Python module; follow `slurm/setup.sh`.
- Reuse one SSH connection to avoid portal bans. Do not repeatedly retry
  logins; direct `portalNN` addresses are not reachable.
- Keep osmium's `simple` strategy and 14 tile extracts per job; other settings
  previously ran out of memory.
