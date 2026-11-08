# Route-pool precompute on the UVA CS cluster

Everything runs from a portal node (`ssh <id>@portal.cs.virginia.edu`);
compute nodes are reached only through SLURM. Each person needs their own CS
account from cshelpdesk.

## Setup

```bash
# laptop, in the repo
rsync -av --exclude .venv --exclude cache --exclude pools --exclude build-cache \
    ./ <id>@portal.cs.virginia.edu:~/runline/
# portal
cd ~/runline && bash slurm/setup.sh          # precompute only
cd ~/runline && BUILD=1 bash slurm/setup.sh  # also tile graph builds
```

## One shipped area (Charlottesville)

```bash
jid=$(sbatch --parsable --export=AREA=charlottesville-va slurm/precompute.sbatch)
sbatch --dependency=afterok:$jid --export=AREA=charlottesville-va slurm/merge.sbatch
```

## The top 50 metros, in batches

`config/metro-batches.json` splits the 50 metros (minus DC, whose pilot ran)
into a test batch of about 74,000 cells and eight batches of 360,000–420,000.
At the pilot's 70 s per cell, each large batch is about 7,800 CPU-hours: about
10 hours if all 800 cores stay busy, closer to 1.5 days at the pilot's
average. Batches split the cells exactly as one run of all 50 would, so no
cell is computed twice.

Once per cluster, download `us-latest.osm.pbf` (about 11 GB,
download.geofabrik.de) to `/bigtemp/$USER`, install osmium-tool (see
`slurm/extract.sbatch`), and keep only the highway ways:

```bash
bash slurm/run_batch.sh --filter
```

Then, when that job succeeds, one command per batch submits all four steps,
each waiting for the one before:

```bash
bash slurm/run_batch.sh test
```

Its pools land in `/bigtemp/$USER/batches/test/pools/pools.sqlite`. Before
the next batch:

- Copy the file somewhere permanent. `/bigtemp` is volatile.
- Check `sacct` for OUT_OF_MEMORY. Tasks ask for 4G, against the pilot's
  2.1 GB peak, and New York and Los Angeles (`b1`) may need more. Rerun with
  `MEM=8G bash slurm/run_batch.sh <name>`: built tiles and finished units are
  skipped.
- If a tile build fails, the precompute stays pending forever
  (`DependencyNeverSatisfied`). `scancel` it, fix the build, and rerun the
  batch.

To keep the cores busy, submit the next batch while the last one's slowest
tasks are finishing.

## The top 50 metros, step by step

What `run_batch.sh` does, for a pilot or a rerun of one step. Work under
`/bigtemp/$USER` (home is 100 GB) and copy the finished `pools.sqlite`
somewhere permanent; `/bigtemp` is volatile.

1. Download `us-latest.osm.pbf` (about 11 GB, download.geofabrik.de) to
   `/bigtemp/$USER`, and install osmium-tool (see `slurm/extract.sbatch`).
2. Cut tile extracts:
   ```bash
   .venv/bin/python scripts/build_tile_graphs.py configs \
       --pbf /bigtemp/$USER/us-latest.osm.pbf --extracts /bigtemp/$USER/tiles
   ```
   It prints two `sbatch` lines: one prefilter job, then an array of cuts of
   about fourteen tiles each (osmium needs ~1.8 GB per tile it cuts at once).
3. Build tile graphs, then check sizes before sizing step 5's memory:
   ```bash
   sbatch --export=EXTRACTS=/bigtemp/$USER/tiles,GRAPHS=/bigtemp/$USER/tile-graphs \
       slurm/build_tiles.sbatch
   .venv/bin/python scripts/build_tile_graphs.py summary --output /bigtemp/$USER/tile-graphs
   ```
4. Plan the pools (`--metro washington-dc` and the like for a pilot):
   ```bash
   .venv/bin/python scripts/plan_tile_pools.py --output /bigtemp/$USER/us-pools
   ```
5. Run it; the merge starts by itself when every array succeeds:
   ```bash
   bash slurm/submit_tiles.sh /bigtemp/$USER/us-pools/plan.json.gz \
       /bigtemp/$USER/tile-graphs /bigtemp/$USER/us-pools
   ```

Check `scontrol show config | grep -i MaxArraySize` first; `submit_tiles.sh`
assumes arrays of 1000. Tell cshelpdesk before holding hundreds of cores
for days.

## Gotchas

- Do not `module load python`: on portal09 the `python/3.12.3` module loads
  without error but points at a directory that does not exist, and Ubuntu's
  `python3` is 3.10. `setup.sh` installs uv's standalone 3.12 instead, and
  jobs run `.venv/bin/python`.
- Shards that exist are skipped. Delete old shards after any code or graph
  change; merge refuses to mix builds anyway.
- Pickled graphs only load under the pinned versions in `requirements.txt`.
- Per user: `cpu` 400 cores and 4 days, `gpu` 400 cores, `nolim` 80 cores and
  20 days. Nothing here needs a GPU, but the array jobs ask for `cpu,gpu`
  so they can use both partitions' cores, up to 800 tasks at once.
- Reservations come and go (`scontrol show reservation`); a job pinned to a
  reserved feature waits until the reservation ends.
