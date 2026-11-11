# Route-pool precompute on SLURM (UVA CS cluster or Rivanna)

Everything runs from a login node; compute nodes are reached only through
SLURM. Up to the DC pilot, everything ran on the UVA CS cluster
(`portal.cs.virginia.edu`) under Krish's account. The scripts also carry a
config for UVA Research Computing's Rivanna/Afton (`login.hpc.virginia.edu`),
where nothing has been run yet: read [Rivanna](#rivanna) and its
[pre-flight checklist](#pre-flight-checklist-for-the-top-50-run) first.

## Cluster configs

What differs between clusters lives in `slurm/clusters/<cluster>.env`, which
`slurm/cluster.sh` loads for every script:

| Setting | `cs.env` | `rivanna.env` |
|---|---|---|
| `PARTITION` | `cpu,gpu` (400 cores each) | `standard` (1000 cores per user) |
| `ACCOUNT` (`-A`) | none | required: `export ACCOUNT=<allocation>` |
| `BASE` (work root) | `/bigtemp/$USER` (volatile) | `/scratch/$USER` (90-day purge) |
| `CONCURRENT`, `MAX_TILE_SHARDS` | 800 | 1000 |
| `MAX_ARRAY` (array size) | 1000 | 1000 (verify `MaxArraySize`) |
| `PRECOMPUTE_TIME` | the sbatch file's 2 days | 12 hours (keeps the SU hold small) |
| `MODULE_PURGE` | no | yes: jobs run `module purge` |
| Python | uv's 3.12 in `.venv` | the same |

The cluster is picked by `CLUSTER=cs` or `CLUSTER=rivanna`, else by host name
(`*.hpc.virginia.edu` is Rivanna), else by whether the RC-only `allocations`
command exists, else `cs`. Any setting can be overridden from the
environment, e.g. `CONCURRENT=500 bash slurm/run_batch.sh b1`.

The `.sbatch` files name no partition or account. Submit through
`run_batch.sh`, `submit_tiles.sh` or `bash slurm/sbatch.sh <sbatch args>`,
which add them; plain `sbatch` needs `-p` (and `-A` on Rivanna).

**Dry run.** `DRY_RUN=1` prints every `sbatch` command instead of submitting
it. The planning steps still run, so array sizes are real; off the cluster
they write to a temporary directory and the printed paths show the cluster's
`BASE`. On a laptop, point `PY` at a venv with the runtime requirements:

```bash
CLUSTER=rivanna ACCOUNT=myalloc DRY_RUN=1 PY=.venv/bin/python bash slurm/run_batch.sh test
CLUSTER=cs DRY_RUN=1 PY=.venv/bin/python bash slurm/run_batch.sh --smoke
```

## Setup

```bash
# laptop, in the repo; <login host> is portal.cs.virginia.edu or login.hpc.virginia.edu
rsync -av --exclude .venv --exclude cache --exclude pools --exclude build-cache \
    ./ <id>@<login host>:~/runline/
# login node
cd ~/runline && bash slurm/setup.sh          # precompute only
cd ~/runline && BUILD=1 bash slurm/setup.sh  # also tile graph builds
```

Keep the repo (and so `.venv`) in home, not scratch: Rivanna purges scratch
files nobody has read in 90 days.

## One shipped area (Charlottesville)

```bash
jid=$(bash slurm/sbatch.sh --parsable --export=ALL,AREA=charlottesville-va slurm/precompute.sbatch)
bash slurm/sbatch.sh --dependency=afterok:$jid --export=ALL,AREA=charlottesville-va slurm/merge.sbatch
```

## The top 50 metros, in batches

`config/metro-batches.json` splits all 50 metros into a test batch of about
74,000 cells and eight larger batches. Washington, DC is included in `b6`
with Baltimore because the signal-preserving graph rebuild invalidates the
old pilot's pools. Batch cell counts are recorded in the config.
At the pilot's 70 s per cell, each large batch is about 7,800 CPU-hours: about
10 hours if 800 cores stay busy, closer to 1.5 days at the pilot's average.

Batches split the cells exactly as one run of all 50 would, so no cell is
computed twice. `done` is empty for this rebuild. For future partial reruns,
`plan_tile_pools.py --done` also supports retaining a compatible standalone
pilot: it reserves that pilot's own boundary cells and reassigns neighboring
cells to the batches that will actually run. Never list the old DC pilot as
done when building the new graphs.

Once per cluster, download `us-latest.osm.pbf` (about 11 GB,
download.geofabrik.de) to `$BASE`, install osmium-tool (see
`slurm/extract.sbatch`), and keep only the highway ways:

```bash
bash slurm/run_batch.sh --filter
```

Then, when that job succeeds, one command per batch submits all four steps,
each waiting for the one before:

```bash
bash slurm/run_batch.sh test
```

Its pools land in `$BASE/batches/test/pools/pools.sqlite`. Before the next
batch:

- Copy the batch off scratch (next section). Both clusters' scratch is
  volatile.
- Check `sacct` for OUT_OF_MEMORY. Tasks ask for 4G, against the pilot's
  2.1 GB peak, and New York and Los Angeles (`b1`) may need more. Rerun with
  `MEM=8G bash slurm/run_batch.sh <name>`: built tiles and finished units are
  skipped.
- If a tile build fails, the precompute stays pending forever
  (`DependencyNeverSatisfied`). `scancel` it, fix the build, and rerun the
  batch.

To keep the cores busy, submit the next batch while the last one's slowest
tasks are finishing.

### Copying a batch off

Pools alone are worthless. Serving checks the sha256 of each tile's
`.pkl.gz`, and rebuilding a tile changes it, so archive the exact tile graphs
the pools were built on, the plan, and the shards (which rebuild
`pools.sqlite`) with every batch:

```bash
# login node: list the batch's files, relative to $BASE
bash slurm/run_batch.sh --files b1 > ~/b1-files.txt
rsync -a --files-from=$HOME/b1-files.txt "$BASE"/ <archive>/runline/
```

`<archive>` is the storage chosen in the checklist (Research Standard on
Rivanna, say). From a laptop over the one SSH connection instead:

```bash
ssh rivanna 'cd ~/runline && bash slurm/run_batch.sh --files b1' > b1-files.txt
rsync -a --files-from=b1-files.txt rivanna:/scratch/<id>/ ~/runline-archive/
```

`--files` warns about any expected file that is missing (an unfinished unit,
say). Check the copy (`du -sh`, a row count of `pools.sqlite`) before
deleting anything on scratch.

## The top 50 metros, step by step

What `run_batch.sh` does, for a pilot or a rerun of one step. Work under
`$BASE` (home is small) and copy the results somewhere permanent.

1. Download `us-latest.osm.pbf` (about 11 GB, download.geofabrik.de) to
   `$BASE`, and install osmium-tool (see `slurm/extract.sbatch`).
2. Cut tile extracts:
   ```bash
   .venv/bin/python scripts/build_tile_graphs.py configs \
       --pbf $BASE/us-latest.osm.pbf --extracts $BASE/tiles
   ```
   It prints two `sbatch` lines (run them with `bash slurm/sbatch.sh`): one
   prefilter job, then an array of cuts of about fourteen tiles each (osmium
   needs ~1.8 GB per tile it cuts at once).
3. Build tile graphs, then check sizes before sizing step 5's memory:
   ```bash
   bash slurm/sbatch.sh --export=ALL,EXTRACTS=$BASE/tiles,GRAPHS=$BASE/tile-graphs \
       slurm/build_tiles.sbatch
   .venv/bin/python scripts/build_tile_graphs.py summary --output $BASE/tile-graphs
   ```
4. Plan the pools (`--metro washington-dc` and the like for a pilot):
   ```bash
   .venv/bin/python scripts/plan_tile_pools.py --output $BASE/us-pools
   ```
5. Run it; the merge starts by itself when every array succeeds:
   ```bash
   bash slurm/submit_tiles.sh $BASE/us-pools/plan.json.gz \
       $BASE/tile-graphs $BASE/us-pools
   ```

Check `scontrol show config | grep -i MaxArraySize` first; `submit_tiles.sh`
assumes arrays of `MAX_ARRAY` (1000). Tell the cluster's support (cshelpdesk,
or RC on Rivanna) before holding hundreds of cores for days.

## Gotchas

- Do not `module load python` (or miniforge on Rivanna): on portal09 the
  `python/3.12.3` module loads without error but points at a directory that
  does not exist, and Ubuntu's `python3` is 3.10. On Rivanna, Python is a
  miniforge module whose version is not pinned to ours. `setup.sh` installs
  uv's standalone 3.12 on every cluster, so the pins match the laptop and
  pickled graphs load anywhere, and jobs run `.venv/bin/python`.
- Shards that exist are skipped. Delete old shards after any code or graph
  change; merge refuses to mix builds anyway.
- Pickled graphs only load under the pinned versions in `requirements.txt`.
- Pools match only the exact tile graphs they were built from. Build pools
  on one cluster only, or copy the tile graphs with them.
- CS cluster, per user: `cpu` 400 cores and 4 days, `gpu` 400 cores, `nolim`
  80 cores and 20 days. Nothing here needs a GPU, but the array jobs ask for
  `cpu,gpu` so they can use both partitions' cores, up to 800 tasks at once.
  Rivanna's `gpu` partition refuses jobs that ask for no GPU, so there it is
  `standard` only.
- Reservations come and go (`scontrol show reservation`); a job pinned to a
  reserved feature waits until the reservation ends.

## Rivanna

Nothing here has been run on Rivanna. The facts below come from UVA RC's
pages (sources at the end; rc.virginia.edu blocks scripted fetches, so they
were read from the site's public source, github.com/uvarc/rc-website, on
2026-11-10). Anything marked **verify on login** could not be confirmed
that way.

### What is different from the CS cluster

- **Partition.** `standard`: one node per job, 7 days max, 9 GB per core by
  default, and at most 1000 cores across a user's running jobs. There is no
  default partition. Each task here is one core and 4 GB, so 1000 run at
  once at most, whatever `CONCURRENT` says.
- **Allocation.** Every job needs `-A <allocation>`; without it sbatch fails
  with "Invalid account or account/partition combination specified". Run
  `allocations` for the name (all lowercase) and balance, then
  `export ACCOUNT=<name>` (put it in `~/.bashrc`). Allocations belong to a
  faculty PI's Grouper group; students cannot request one.
- **Service units.** One SU is one core-hour on `standard`. A job asking
  for more memory than its cores times the per-core memory is charged for
  the extra cores, so the 32G, 2-core extract tasks and the 16G merge cost
  more than their cores (the exact rounding is **verify on login**); the 4G
  and 8G precompute tasks do not. When a job is submitted, SUs for its whole
  requested time are held, and the unused part is released when it ends; a
  submission fails if the hold does not fit the available balance. That is
  why `rivanna.env` sets `PRECOMPUTE_TIME=12:00:00`: DC's longest task took
  358 min, and 4,000 tasks at 2 days would hold 192,000 SUs, at 12 hours
  48,000. Whether an array is held for every task at submission or per task
  as it starts is **verify on login** (submit the smoke test and watch the
  `Reserved` column of `allocations`).
- **Storage.** Home is 200 GB. `/scratch/$USER` is 10 TB, not backed up,
  and files not read or written for 90 days are deleted daily, empty
  directories too. Going over the scratch quota blocks job submission
  (`hdquota -s` shows usage). `check-scratch-for-purge > list.txt` lists what
  is due to go. Research Standard storage (`/standard/<group>`) is leased
  per PI group; each PI gets up to 10 TB free.
- **Python and osmium.** The same as on the CS cluster: `setup.sh` installs
  uv's standalone 3.12 into home, and osmium-tool goes into
  `~/.local/osmium` with micromamba (`extract.sbatch`). Jobs `module purge`
  first so a loaded miniforge cannot shadow `.venv`. Run setup on a login
  node; if it is killed for using too much memory, run it in an interactive
  job: `ijob -A $ACCOUNT -p interactive -c 1 --mem=8G --time=2:00:00`.
- **Maintenance.** The whole system goes down one day a quarter (2026: Jan
  6, May 26, Aug 11, Oct 13; 2027 dates **verify on login** or on the RC
  site). A job that cannot finish before maintenance starts is held until
  after it, another reason for 12-hour tasks.
- **Login node limits.** Processes that exceed them are killed. Planning a
  batch peaks around 1.8 GB for a few seconds on a laptop; if it is killed,
  run `run_batch.sh` from an `ijob`. Whether `sbatch` works from inside an
  `ijob` is **verify on login**.

Also **verify on login**: `MaxArraySize` (`scontrol show config | grep -i
MaxArraySize`; lower `MAX_ARRAY` if under 1001), per-user job or submit
limits beyond the 1000 cores (`qlimits`, `sacctmgr show qos`), that compute
nodes can reach the internet (the tile builds fetch Copernicus elevation
tiles from AWS; the smoke test proves it), that `git` exists on compute nodes
(shard headers record the commit, else "unknown"), and the frontend host
names behind `login.hpc.virginia.edu`.

### SSH

- The host is `login.hpc.virginia.edu`; the login is your UVA computing ID
  and Eservices password. Off Grounds, connect UVA Anywhere (the VPN) first,
  or ssh hangs with no message. Key login is allowed from UVA addresses.
- The CS portal blocks repeated connections and failed logins. Whether RC
  does the same is **verify on login**, so behave as if it does: one
  multiplexed connection, and check progress rarely (twice a day is
  plenty; jobs need no babysitting). In `~/.ssh/config`:

  ```
  Host rivanna
      HostName login.hpc.virginia.edu
      User <computing id>
      ControlMaster auto
      ControlPath ~/.ssh/cm-%r@%h:%p
      ControlPersist 8h
      ServerAliveInterval 60
  ```

  Then `ssh rivanna` once; every later `ssh rivanna ...` and
  `rsync ... rivanna:` reuses it without a new login.
- `login.hpc.virginia.edu` likely spreads users over several frontends, like
  the CS `portal` alias (**verify on login**: run `hostname`). A `tmux`
  session lives on one frontend, so do not rely on one; nothing here needs
  it, since SLURM holds the state and `progress.sh` is stateless.
- Open OnDemand (`ood.hpc.virginia.edu`) has a shell, a file browser and a
  job monitor in the browser, and its Desktop app needs no VPN. Use it for
  progress checks instead of new SSH logins.

### Runbook

Every command runs on a login node in `~/runline` unless it says laptop.

0. **Pre-flight.** Work through the checklist below. Do not start step 4
   until every branch it names has merged.
1. **Setup.** From the laptop, rsync the repo (see Setup), then on Rivanna:
   ```bash
   allocations                                  # note the allocation name and balance
   echo 'export ACCOUNT=<allocation>' >> ~/.bashrc && source ~/.bashrc
   cd ~/runline && BUILD=1 bash slurm/setup.sh  # should say "setup done on rivanna"
   curl -Ls https://micro.mamba.pm/api/micromamba/linux-64/latest | tar -xvj -C ~/.local bin/micromamba
   ~/.local/bin/micromamba create -y -p ~/.local/osmium -c conda-forge osmium-tool
   DRY_RUN=1 bash slurm/run_batch.sh test       # prints standard, -A, /scratch/$USER
   ```
2. **Smoke test, small area.** Two shards of Charlottesville's 400 on the
   shipped graph, about 11 cells each: tests the account, partition, venv,
   pickles and `git` without any download. A few SUs.
   ```bash
   jid=$(bash slurm/sbatch.sh --parsable --array=0-1 --time=01:00:00 \
       --export=ALL,AREA=charlottesville-va slurm/precompute.sbatch)
   bash slurm/sbatch.sh --dependency=afterok:$jid \
       --export=ALL,AREA=charlottesville-va,SHARDS=2 slurm/merge.sbatch
   ```
   Check `slurm/logs/`, `sacct -j $jid`, and `allocations` (the `Reserved`
   column). Then `rm -r pools/charlottesville-va*`: those two shards are
   real, but the merged file holds only 22 cells.
3. **Smoke test, two tiles.** Download the extract to scratch (11 GB; on a
   login node with `wget -c`, or in a job if downloads are throttled there),
   prefilter it, then run every step of the metro pipeline on two tiles of
   Salt Lake City with 3 cells per unit, in `$BASE/smoke`:
   ```bash
   cd /scratch/$USER && wget -c https://download.geofabrik.de/north-america/us-latest.osm.pbf
   cd ~/runline && bash slurm/run_batch.sh --filter
   # when the filter job is done:
   bash slurm/run_batch.sh --smoke
   ```
   One extract cut, two tile builds, two precompute tasks and a merge, a few
   core-hours in all. When the merge is done, check
   `/scratch/$USER/smoke/pools/pools.sqlite`, the build logs (elevation tiles
   downloaded, so compute nodes reach the internet), and `sacct` for the
   memory each step used. Then `rm -r /scratch/$USER/smoke`.
4. **Test batch** (three metros, 74,000 cells, about 1,450 core-hours, 1,604
   tasks), once the branches in the checklist have merged and been pulled
   and rsynced:
   ```bash
   bash slurm/run_batch.sh test
   bash slurm/progress.sh /scratch/$USER/batches/test/tiles \
       /scratch/$USER/tile-graphs /scratch/$USER/batches/test/pools   # now and then
   ```
   When the merge finishes: copy the batch off (above), check `sacct` for
   OUT_OF_MEMORY and TIMEOUT, run `build_tile_graphs.py summary --output
   /scratch/$USER/tile-graphs` and scale its MB by 8,447 / 222 tiles to fix
   the storage estimate, and compare the SUs used (`mam-list-transactions -a
   $ACCOUNT -s <start date>`) with the estimate below.
5. **The eight batches**, `b1` to `b8` in order, each about 7,800
   core-hours (8 hours or so at 1000 cores, longer as fairshare lowers
   priority):
   ```bash
   MEM=8G bash slurm/run_batch.sh b1      # New York and Los Angeles may need 8G
   # ... merge done: copy b1 off, check sacct, then
   bash slurm/run_batch.sh b2
   ```
   After every batch, copy it off before starting the next, and before
   going quiet for long: anything left on scratch for 90 days unread is
   gone. Keep `us-latest.osm.pbf` and `highways.osm.pbf` until `b8` is cut;
   every batch's extracts are cut from them, and each batch's `tiles/`
   extracts can be deleted once its tile graphs are built.
6. **Afterwards.** Check the archive against `--files` for every batch,
   then clear `/scratch/$USER`. Note the run (cluster, commit, dates, SUs)
   in `docs/handoff.md`.

## Pre-flight checklist for the top 50 run

**Wait for the code first.** Do not start the test batch or the eight
batches until all of these have merged into `main`, been pulled, and been
rsynced to the cluster (then rerun `setup.sh`):

- `serve-metro-tiles` (handoff task 2): serving metro pools, deterministic
  tile graph gzip and a tile column in the merge, so it changes what the
  run writes.
- `pool-hit-rate` (task 3): drops loops serving would reject before they
  fill pool slots, and picks cells from the routable core, so it changes
  every pool.
- The signal-tag fix: it changes tile graphs, so every tile graph must be
  rebuilt. The old DC pilot is incompatible with rebuilt graphs; the config
  already schedules Washington with Baltimore in `b6` and leaves `done`
  empty. Preserve the old graph/pool pair only as an archive.

Pools from code before these land are thrown away, so only the smoke tests
(runbook steps 2 and 3) are worth running before then. If old shards or
tile graphs are on scratch when the code changes, delete them: existing
shards and tile graphs are skipped, not rebuilt.

**Allocation and SUs.**

- [ ] Run `allocations` on Rivanna: which allocation you are in, its
      balance, and its expiry (`allocations -a <name>`; standard
      allocations last 12 months). Your access came through some PI's
      Grouper group; find out whose, since its usage lowers that group's
      queue priority (fairshare) too.
- [ ] Confirm about **150,000 SUs** available, or ask the PI to renew or
      request a standard allocation (free; only faculty can request). The
      sum: about 65,000 core-hours of precompute at 1 SU each, a guessed
      5,000–10,000 for tile builds (not measured; the test batch's
      `summary` gives build-hours), about 2,000 for extracts (32G jobs are
      charged as about four cores), merges negligible, plus 25% for reruns:
      about 100,000 used. On top, each big batch holds up to 48,000 SUs
      (4,000 tasks at 12 hours) at submission, so near the end the balance
      must cover what is left plus that hold. Lower `PRECOMPUTE_TIME` (say
      `8:00:00`, if the test batch's longest task allows) to shrink it.
      Adding DC back adds about 1,800.

**Storage** (estimates; fix them after the test batch):

| What | Size | Where |
|---|---|---|
| `us-latest.osm.pbf` + `highways.osm.pbf` | 11 GB + a few | scratch, until `b8` is cut |
| Tile extracts (`batches/*/tiles`) | not measured | scratch, delete per batch |
| Tile graphs, 8,447 tiles | 85–340 GB (guess below) | scratch, then archive |
| Shards (`*.jsonl.gz`) | about the pools again, 85–115 GB | scratch, then archive |
| `pools.sqlite`, all batches | about 85 GB | scratch, then archive |
| `plan.json.gz` | a few MB each | scratch, then archive |

The tile graph guess: Charlottesville's 12 km graph is 7.9 MB gzipped; a
tile is 13.8 km and most are denser, so 10–40 MB a tile. Archive in all is
then roughly **260–540 GB**, more than home's 200 GB. Scratch at its peak
holds the same plus the extracts, far under its 10 TB.

- [ ] Choose the archive: Research Standard storage for the PI's group
      (`/standard/<group>`, up to 10 TB free per PI, requested by the PI)
      is the obvious one, since it is on the cluster and not purged. A
      second copy off UVA (Drive, or Azure Blob from task 1) for the files
      serving needs: `pools.sqlite`, `plan.json.gz`, tile graphs.
- [ ] Make sure nothing waits on scratch more than a few weeks; the 90-day
      purge counts from the last read or write.

**People to tell.**

- [ ] The PI whose allocation you use, before spending it: about 100,000
      SUs over one to three weeks, up to 1000 cores at once.
- [ ] RC user services (https://www.rc.virginia.edu/support/), before
      holding 1000 cores for days: they can say whether a lower
      `CONCURRENT`, other hours, or a different partition suits them.
- [ ] Krish and Adithya: which cluster the run is on, so nobody builds pools
      on the other one with different tile graphs.

**Checks on first login** (the **verify on login** items above):
`allocations`, `qlist`, `qlimits`, `scontrol show config | grep -i
MaxArraySize`, `hdquota -s`, `hostname`, `which git`, and whether a
`--parsable` array submission holds SUs for all its tasks at once.

## Sources

UVA Research Computing, read from the site's source (github.com/uvarc/rc-website)
on 2026-11-10:

- Partitions, the 1000-core limit, no default partition, the memory charge,
  login node limits, scratch policy: https://www.rc.virginia.edu/userinfo/hpc/#job-queues
  (live table with SU rates: https://s3.amazonaws.com/uvarc-resources/website/qinfo.csv)
- `-A`, `qlist`, `qlimits`, `ijob`, `list-group-usage`, `mam-list-transactions`:
  https://www.rc.virginia.edu/userinfo/hpc/slurm/
- `allocations`, SU holds, the "Invalid account" error, `hdquota -s`:
  https://www.rc.virginia.edu/userinfo/faq/rivanna-faq/
- SU = core-hour, standard allocations (free, 12 months, PI only, Grouper):
  https://www.rc.virginia.edu/userinfo/hpc/access/
- Home 200 GB, scratch 10 TB and 90 days: https://www.rc.virginia.edu/userinfo/hpc/storage/
- The purge and `check-scratch-for-purge`:
  https://www.rc.virginia.edu/2024/07/reinstatement-of-file-purging-of-personal-/scratch-files-on-afton-and-rivanna/
- Research Standard, 10 TB free per PI: https://www.rc.virginia.edu/userinfo/storage/research-standard/
- Login host, VPN, keys: https://www.rc.virginia.edu/userinfo/hpc/login/ and
  https://www.rc.virginia.edu/userinfo/hpc/logintools/rivanna-ssh/
- Python is a miniforge module: https://www.rc.virginia.edu/userinfo/hpc/software/python/
- 2026 maintenance days: https://www.rc.virginia.edu/2026/01/hpc-maintenance-schedule-for-2026/
  (URL inferred from the site's pattern; the post is
  `content/post/2026-hpc-maintenance-dates.md` in the source)
