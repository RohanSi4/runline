# Handoff: where RunLine stands

Written for Rohan when Krish handed off the remaining work, as of `24402ba`.

## State

| Piece | State |
|---|---|
| Pool serving (`src/runline/serving.py`) | Merged. Works locally. The API's `source` field says `pool` or `live` |
| Deployed app | Runs that code, but always `live`: no pool file is hosted |
| Charlottesville pools | Built: 13,092 pools, 110 MB. Not hosted |
| DC pilot | Finished: 72,237 cells with pools, 20.25M loops, 2.3 GB. Serving can't read this file yet (task 2) |
| Top 50 metros | Not run |

Pool files are gitignored. Krish shares them in a Drive folder, sent separately.
The originals are in Krish's `/bigtemp` on the cluster, which is volatile, so
treat the Drive copy as the one to keep.

## Check it works

Put the Charlottesville file at `pools/charlottesville-va.sqlite`, then:

```bash
.venv/bin/python -m pytest -q
.venv/bin/python scripts/compare_pools.py --area charlottesville-va --starts 40
```

Last result, across 200 requests: 71% answered from pools, 14–23 ms against
0.6–2.4 s live, with similar route quality (the README has the table). Without
the file, requests plan live with no error. If you rebuild the graph, the old
pool is ignored and a warning is logged.

## Remaining work, in order

### 1. Host the pools

Vercel's 250 MB limit rules it out, and serving needs the pool file and graph on
local disk. Google Drive or a storage bucket works as a backup, not for serving.
The suggested host is a DigitalOcean droplet from the GitHub Student Pack.

- Run the whole app there, since FastAPI already serves `web/`. Otherwise the
  Vercel page has to call another origin and the API needs CORS.
- Set `RUNLINE_POOLS_DIR` if the files live outside the repo's `pools/`.
- Done when a deployed request returns `source: pool`.

### 2. Serve DC

Copying the file is not enough. Serving assumes one area, one graph and one
pool file. A metro run differs:

- `merge_pools.py` writes each tile's graph hash to a `tiles` table and no
  `graph` entry to `meta`, so `_checked_pool_file` rejects the file.
- A pool's node ids belong to its tile's graph, so serving must find the cell's
  owning tile (`runline.tiling.owned_cells`, or the run's `plan.json.gz`) and
  load that graph.
- `containing_area` and `/api/areas` only know areas in the graph manifest.

This needs DC's tile graphs and plan as well as the pools. Size the server by
measuring tile graph load time and memory first. They have not been measured
for serving, and the top 50 metros have 8,675 tiles.

### 3. Raise the hit rate

Of the 58 misses in 200 requests, 40 had no connector within 300 m
(`MAX_CONNECTOR_METERS`), from 8 of the 40 starts, and 18 had fewer than 3 loops
in the distance band.

- Start the loop at its nearest point to the runner, rather than adding a
  connector out and back.
- Count signals and crossings on connectors. They are currently left out, so
  pool quality figures read slightly better than they should.
- Consider returning 2 routes when only 2 are in band.

### 4. Run the top 50 metros

Follow `slurm/README.md`. The estimate, scaled from DC, is about 65,000 CPU-hours
and 85 GB: 3.4 days at 800 cores, or about 11 days at DC's average of 245.

Before starting:

- Choose a cluster and check access early, because everything here ran under
  Krish's CS account. Either get your own CS account and confirm the 800-task
  limit (400 on `cpu`, 400 on `gpu`), or use Rivanna (see below).
- Tell cshelpdesk before holding that many cores for days.
- Download the 11 GB US extract, and plan where 85 GB will live. Home is 100 GB
  and `/bigtemp` is volatile.

**Rivanna.** Krish believes you have access to UVA Research Computing's Rivanna,
so confirm that first. It could replace the CS cluster, but nothing has been run
or tested there:

- `slurm/` is written for the CS cluster, so partition names, `/bigtemp` paths
  and core limits all need changing. Rerun `setup.sh` and test it on a small area.
- Check that your allocation covers about 65,000 core-hours, how many jobs can run
  at once, and the scratch space's size and purge rules.
- Rebuild pools on one cluster only unless you copy the graphs, since a pool only
  matches the exact graph it was built from.

Apply what DC taught us. Neither change is in the code yet:

- Lower `#SBATCH --mem` in `slurm/precompute_tiles.sbatch` from 8G to 4G. Peak
  use was 2.1 GB, and asking for less gets more tasks scheduled.
- Plan with `--cells-per-unit 50` rather than 100. The p95 task took 256 min and
  the longest 358, so smaller units save more when a task fails.

Consider whether task 2 changes the pool format before spending this compute.

## Traps

- **SSH bans.** Repeated connections or failed logins get you automatically
  blocked from the portal. Reuse one connection (`ControlMaster`) and check
  progress rarely. The `portal` alias also rotates nodes, and direct `portalNN`
  addresses aren't reachable.
- **Tile extracts.** Keep osmium's `simple` strategy and 14 tiles per job, as
  `build_tile_graphs.py` does. Other settings ran out of memory at up to 64 GB.
- **Repo rules.** `CLAUDE.md` asks for a branch and PR per task, with benchmark
  numbers for changes to routing code, and no real coordinates in tracked files.
