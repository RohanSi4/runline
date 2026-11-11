# RunLine handoff

## Unified public app

The original URL now uses the combined API: rebuilt Charlottesville locally,
DC through the existing Oracle regional service. See
[unified-deployment.md](unified-deployment.md) for deployment/rollback and
[presentation-script.md](presentation-script.md) for the click-through.
The earlier deployment records below remain as history.

## Presentation preparation

Integration PR #5 is merged. Presentation work is on
`codex/presentation-ready` in the `runline-presentation` checkout, tracked by
PR #6. See [presentation.md](presentation.md) for the demo sequence and
[presentation-release.md](presentation-release.md) for its measured results.
The rebuilt graph and complete 11,417-pool, 605,206-loop dataset are audited
and deployed to the capstone URL. All 96 shards finished without failures;
the complete package and audit are attached to the capstone draft release.
A separate public presentation deployment is available at
https://runline-capstone.vercel.app, with a hosted recorded fallback. The Oracle service was not modified in this session. The team subsequently
recorded its migration to main and the indexed DC layout below.

## Recovery status

Claude's November 10 work is integrated on `codex/resume-runline` in the
`runline-recovery` checkout. The original five branches and their worktrees
are preserved. The integration includes upstream `32a7876`, which hosts the existing API
on Oracle. This recovery has not changed the deployed backend.

| Work | Current state |
|---|---|
| Current hosting | The capstone project serves rebuilt Charlottesville directly on Vercel. The original Vercel/Oracle app serves Charlottesville and DC from `main` (task 4); both were verified through its public API. |
| Optional hosting tools | Provisioning, systemd, Caddy, area-pool upload, backup and smoke scripts integrated as a portable VM recipe. No Azure VM is needed for the existing deployment. |
| Metro serving | Cell-to-tile index, graph hash checks, bounded graph cache and live fallback integrated. Serving the DC pilot on the VM; measurements in task 4. |
| Pool hit rate | Loops start where they pass nearest the runner; requests read their cell and eight neighbors and measure the entire drawn route. Metro reads are restricted to the loaded tile. |
| Routing fixes | Builders retain signalized crosswalk tags and nodes; nearby signal nodes count as one event; self-crossing loop area counts enclosed faces. Browser validation errors display as text. |
| Cluster precompute | Batch `b1` (New York and Los Angeles) finished on the UVA CS cluster on November 11 and is served from the Oracle VM (task 4). Other batches have not started. Rivanna allocation has not been verified. |
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

`RUNLINE_TILE_GRAPHS_CACHE` defaults to two graphs per worker.

Tile graphs can instead live in object storage (prototype, off by default).
Set `RUNLINE_TILE_GRAPHS_URL` to a base URL laid out like the metros
directory (`<base>/<run>/tile-graphs/<tile>.pkl.gz`) or a template with
`{run}`, `{tile}` and `{graph}`; it must be HTTPS. A graph missing locally
is then downloaded once (per-tile lock, shared across workers by a lock
file), checked against the hash `pools.sqlite` records for that tile, and
renamed into `RUNLINE_TILE_DISK_CACHE_DIR` (default
`$RUNLINE_CACHE_DIR/tile-graphs`). `RUNLINE_TILE_DISK_CACHE_GB` (default 20)
bounds that cache; least recently used files go first, never one held in
memory. `RUNLINE_TILE_FETCH_TIMEOUT` (default 60 s) bounds a download. A
failed or mismatched fetch is a 503 and is not retried for 15 s. The pool
file itself must still be local. With the URL unset, nothing changes.

The VM service
recipe uses `/srv/runline/pools`, `/srv/runline/metros`, and `/var/cache/runline`;
the existing Oracle service paths are recorded in task 4.
Measure the actual metro graphs before sizing a VM; Charlottesville memory
measurements do not establish DC's memory needs.

## Remaining work, in order

### 1. Source integration

PR #5 is merged. The recovery branch brought together `host-pools-vm`, `serve-metro-tiles`,
`pool-hit-rate`, `signal-tags-and-metrics`, and the already combined
`rivanna-slurm-on-metro`. Validation results belong with its PR. Do not push
to `main` directly.

### 2. Preserve the DC deployment and archive the existing artifacts

Krish's Charlottesville file has 13,092 pools, about 110 MB. The DC pilot
has 72,237 cells with pools, about 20.25 million loops, and a 2.3 GB SQLite
file. Those are historical build figures, not locally verified artifacts.
Krish shared a Drive folder separately; the complete files are not in this
checkout. Keep the exact matching graphs and `plan.json.gz` with each pool.
The originals on the CS cluster's `/bigtemp` are volatile.

The deployed DC file has been indexed (task 4). For an unindexed archive,
add the index using its exact plan:

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
top-50 coverage remains incomplete; batch `b1` (New York and Los Angeles) is served from the Oracle VM.

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
| `~/metros/b1/pools.sqlite`, `plan.json.gz` | Batch `b1` (New York and Los Angeles), indexed with `--add-index` |
| `~/metros/b1/tile-graphs` | Symlink to the same shared folder, which also holds `b1`'s 906 tile graphs (18 GB in total with DC's) |

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

Batch `b1` on the VM (November 11): the merged file passed `PRAGMA
quick_check` (278,729 cells, 836,185 pools, 9.7 GB). `--add-index` with its own
plan indexed 372,064 cells on 906 tiles in about 6 minutes, and all 906 tile
graph hashes match. Through runline-nine.vercel.app, 3–8 mile requests at
Central Park, Prospect Park, Griffith Park, Santa Monica and Long Beach came
from pools (first request in a tile 2–11 s, then about 0.5 s); a Jersey City
road request planned live. With tiles from four regions loaded the process
used about 4.9 GB. A backup of `b1`'s pools and plan is in the portal home
directory under `~/pools/b1`, since `/bigtemp` is volatile.

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
