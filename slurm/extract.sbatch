#!/bin/bash
# Cut the US extract into one small .osm.pbf per metro tile (osmium-tool).
#
# slurm/run_batch.sh submits it. By hand, with BASE as in slurm/clusters/*.env:
#
#   python scripts/build_tile_graphs.py configs \
#       --pbf $BASE/us-latest.osm.pbf --extracts $BASE/tiles
#   f=$(bash slurm/sbatch.sh --parsable --export=ALL,EXTRACTS=$BASE/tiles slurm/extract.sbatch)
#   bash slurm/sbatch.sh --dependency=afterok:$f --array=0-<cuts - 1> \
#       --export=ALL,EXTRACTS=$BASE/tiles slurm/extract.sbatch
#
# us-latest.osm.pbf is about 11 GB from download.geofabrik.de/north-america.
# osmium-tool is not a pip package; install it once into the home directory
# with micromamba (no root needed):
#
#   curl -Ls https://micro.mamba.pm/api/micromamba/linux-64/latest | tar -xvj -C ~/.local bin/micromamba
#   ~/.local/bin/micromamba create -y -p ~/.local/osmium -c conda-forge osmium-tool
#
# OSMIUM_PREFIX in slurm/clusters/*.env says where it is (~/.local/osmium).
#
# Two steps. Without an array it runs filter.sh, keeping only highway ways
# (11 GB down to a few). As an array, task i runs line i+1 of cuts.txt: one
# osmium extract of about fourteen tiles, ~1.8 GB each for its node id set.
# build_tile_graphs.py configs prints both sbatch lines.
#SBATCH --job-name=runline-extract
# No partition or account here: submit through run_batch.sh, submit_tiles.sh
# or sbatch.sh, which add them from slurm/clusters/<cluster>.env.
#SBATCH --cpus-per-task=2
#SBATCH --mem=32G
#SBATCH --time=06:00:00
#SBATCH --output=slurm/logs/%x-%A_%a.out
set -euo pipefail
: "${EXTRACTS:?pass --export=ALL,EXTRACTS=<directory from build_tile_graphs.py configs>}"

cd "$SLURM_SUBMIT_DIR"
# shellcheck source=cluster.sh
source slurm/cluster.sh
job_prelude
export PATH="$OSMIUM_PREFIX/bin:$PATH"
command -v osmium >/dev/null || { echo "osmium-tool not found; see the install note above"; exit 1; }
if [ -n "${SLURM_ARRAY_TASK_ID:-}" ]; then
    sed -n "$((SLURM_ARRAY_TASK_ID + 1))p" "$EXTRACTS/cuts.txt" | bash -euo pipefail
else
    bash "$EXTRACTS/filter.sh"
fi
