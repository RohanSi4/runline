#!/bin/bash
# Cut the US extract into one small .osm.pbf per metro tile (osmium-tool).
#
#   python scripts/build_tile_graphs.py configs \
#       --pbf /bigtemp/$USER/us-latest.osm.pbf --extracts /bigtemp/$USER/tiles
#   f=$(sbatch --parsable --export=ALL,EXTRACTS=/bigtemp/$USER/tiles slurm/extract.sbatch)
#   sbatch --dependency=afterok:$f --array=0-<cuts - 1> \
#       --export=ALL,EXTRACTS=/bigtemp/$USER/tiles slurm/extract.sbatch
#
# us-latest.osm.pbf is about 11 GB from download.geofabrik.de/north-america.
# osmium-tool is not a pip package; install it once into the home directory
# with micromamba (no root needed):
#
#   curl -Ls https://micro.mamba.pm/api/micromamba/linux-64/latest | tar -xvj -C ~/.local bin/micromamba
#   ~/.local/bin/micromamba create -y -p ~/.local/osmium -c conda-forge osmium-tool
#
# Two steps. Without an array it runs filter.sh, keeping only highway ways
# (11 GB down to a few). As an array, task i runs line i+1 of cuts.txt: one
# osmium extract of about fourteen tiles, ~1.8 GB each for its node id set.
# build_tile_graphs.py configs prints both sbatch lines.
#SBATCH --job-name=runline-extract
#SBATCH --partition=cpu,gpu
#SBATCH --cpus-per-task=2
#SBATCH --mem=32G
#SBATCH --time=06:00:00
#SBATCH --output=slurm/logs/%x-%A_%a.out
set -euo pipefail
: "${EXTRACTS:?pass --export=ALL,EXTRACTS=<directory from build_tile_graphs.py configs>}"

cd "$SLURM_SUBMIT_DIR"
export PATH="$HOME/.local/osmium/bin:$PATH"
command -v osmium >/dev/null || { echo "osmium-tool not found; see the install note above"; exit 1; }
if [ -n "${SLURM_ARRAY_TASK_ID:-}" ]; then
    sed -n "$((SLURM_ARRAY_TASK_ID + 1))p" "$EXTRACTS/cuts.txt" | bash -euo pipefail
else
    bash "$EXTRACTS/filter.sh"
fi
