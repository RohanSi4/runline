# RunLine handoff

## Recovery status

Claude's November 10 work is integrated on `codex/resume-runline` in the
`runline-recovery` checkout. The original five branches and their worktrees
are preserved. The integration includes upstream `32a7876`, which hosts the existing API
on Oracle. This recovery has not changed the deployed backend.

| Work | Current state |
|---|---|
| Current hosting | Charlottesville and the DC pilot are served from Krish's Oracle VM, running `main`; Vercel proxies `/api` (task 4). |
| Optional hosting tools | Provisioning, systemd, Caddy, area-pool upload, backup and smoke scripts integrated as a portable VM recipe. No Azure VM is needed for the existing deployment. |
| Metro serving | Cell-to-tile index, graph hash checks, bounded graph cache and live fallback integrated. Serving the DC pilot on the VM; measurements in task 4. |
| Pool hit rate | Loops start where they pass nearest the runner; requests read their cell and eight neighbors and measure the entire drawn route. Metro reads are restricted to the loaded tile. |
| Routing fixes | Builders retain signalized crosswalk tags and nodes; nearby signal nodes count as one event; self-crossing loop area counts enclosed faces. Browser validation errors display as text. |
| Rivanna | Cluster configuration, dry-run and smoke support integrated. No jobs submitted. |
| Graphs and pools | Shipped Charlottesville graph is unchanged. The signal-preserving builders have not rebuilt production graphs or pools. |

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

### 1. Review and merge the integrated code

The recovery branch brings together `host-pools-vm`, `serve-metro-tiles`,
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
routable cells. Production rebuilds have not run during this recovery.

### 4. Update the existing hosted backend

Krish already completed pool hosting in upstream PR #4. Vercel serves the
page and proxies `/api/*` to an Oracle VM at
`https://129-153-47-234.sslip.io`. A health check and an actual five-mile
request confirmed the deployed API works during this recovery. The proxy in
`vercel.json` is preserved.

The server is Ubuntu 24.04 on `VM.Standard.A1.Flex` (ARM), 4 OCPUs, 24 GB
RAM and 190 GB disk, in Krish's Oracle account. Since November 11 it serves
Charlottesville and the DC pilot from `main`:

| Path | What |
|---|---|
| `~/runline-main` | Checkout of `main` that `runline.service` runs (uvicorn `app:app` on `127.0.0.1:8765`, behind Caddy). Its `.venv` links to `~/runline/.venv` |
| `~/runline/pools/charlottesville-va.sqlite` | Area pool (`RUNLINE_POOLS_DIR=~/runline/pools`) |
| `~/metros/washington-dc/pools.sqlite` | DC pilot pools, indexed with `merge_pools.py --add-index` (`RUNLINE_METROS_DIR=~/metros`) |
| `~/metros/washington-dc/tile-graphs` | Symlink to `~/runline/pools/tile-graphs`: the pilot's 230 tile graphs, 4.2 GB |

The service also sets `RUNLINE_ALLOW_OSM_DOWNLOAD=0`,
`RUNLINE_TILE_GRAPHS_CACHE=16` and `RUNLINE_CACHE_DIR=~/.cache/runline`.
`/etc/systemd/system/runline.service.pr7-backup` is the previous unit, which
ran the closed PR #7 branch from `~/runline` (kept for rollback only).

To update after a merge: `git -C ~/runline-main fetch origin main && git -C
~/runline-main checkout --detach origin/main`, then `sudo systemctl restart
runline`. Confirm the commit and a request's `source` afterwards. A graph
rebuild also requires its matching pool file; an old pool will be ignored.

DC pilot on the VM (measured November 11):

- The pilot's `plan.json.gz` was lost from `/bigtemp`. It was rebuilt with
  `plan_tile_pools.py --metro washington-dc` from the unchanged
  `config/metros.json` (93,183 cells on 230 tiles), and `--add-index` accepted
  it: every pooled cell is in it and all 230 tile graph hashes match. The pool
  file passed `PRAGMA quick_check` (72,237 cells, 216,711 pools).
- A tile graph has 49k–124k nodes, loads in 2–4 s and holds about 650 MB once
  serving has used it, so 16 cached tiles are about 10.5 GB.
- Through runline-nine.vercel.app: 2–8 mile requests at six DC-area starts
  came from pools; the first request in a tile takes 6–9 s, later ones
  0.2–0.9 s. 15 miles planned live in about 31 s with the limited-coverage
  warning. Charlottesville 3 and 5 miles also came from pools.
- These are the old pilot's pools and graphs, which match each other. The
  signal-preserving rebuild (task 5, `b6`) will replace them.

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
