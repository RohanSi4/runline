# RunLine handoff

## Recovery status

Claude's November 10 work is integrated on `codex/resume-runline` in the
`runline-recovery` checkout. The original five branches and their worktrees
are preserved. `main` has not been merged or deployed from this checkout.

| Work | Current state |
|---|---|
| VM hosting | Provisioning, systemd, Caddy, area-pool upload, backup and smoke scripts integrated. Azure deployment remains to be done. |
| Metro serving | Cell-to-tile index, graph hash checks, bounded graph cache and live fallback integrated. Tested with cropped Charlottesville tile graphs; actual DC artifacts are unavailable locally. |
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

### 4. Host and verify

Follow [deploy/README.md](../deploy/README.md). The last recorded Vercel
deployment serves Charlottesville live; this recovery did not redeploy it.
The runbook covers provisioning, upload, backups and rollback. Hosting is
done only when a deployed request returns `source: pool`.

Before serving DC, measure its actual tile graphs:

```bash
.venv/bin/python scripts/measure_tile_graphs.py metros/washington-dc/tile-graphs --together 2
```

Keep the complete app on one origin, or explicitly implement CORS if the
frontend and API are hosted separately. The hosting runbook's cloud pricing
and eligibility notes must be checked against the selected subscription
before provisioning.

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
