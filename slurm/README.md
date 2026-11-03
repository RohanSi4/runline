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

## The top 50 metros

Work under `/bigtemp/$USER` (home is 100 GB) and copy the finished
`pools.sqlite` somewhere permanent; `/bigtemp` is volatile.

1. Download `us-latest.osm.pbf` (about 11 GB, download.geofabrik.de) to
   `/bigtemp/$USER`, and install osmium-tool (see `slurm/extract.sbatch`).
2. Cut tile extracts:
   ```bash
   .venv/bin/python scripts/build_tile_graphs.py configs \
       --pbf /bigtemp/$USER/us-latest.osm.pbf --extracts /bigtemp/$USER/tiles
   ```
   It prints two `sbatch` lines: one prefilter job, then an array of cuts of
   about ten tiles each (osmium needs ~1.8 GB per tile it cuts at once).
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
