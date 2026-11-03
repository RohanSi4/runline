#!/bin/bash
# Cut the US extract into one small .osm.pbf per metro tile (osmium-tool).
#
#   python scripts/build_tile_graphs.py configs \
#       --pbf /bigtemp/$USER/us-latest.osm.pbf --extracts /bigtemp/$USER/tiles
#   sbatch --export=EXTRACTS=/bigtemp/$USER/tiles slurm/extract.sbatch
#
# us-latest.osm.pbf is about 11 GB from download.geofabrik.de/north-america.
# osmium-tool is not a pip package; install it once into the home directory
# with micromamba (no root needed):
#
#   curl -Ls https://micro.mamba.pm/api/micromamba/linux-64/latest | tar -xvj -C ~/.local bin/micromamba
#   ~/.local/bin/micromamba create -y -p ~/.local/osmium -c conda-forge osmium-tool
#
# One job, not an array: each osmium run reads the filtered file once and
# writes a few hundred tiles, and the runs share the highway prefilter.
#SBATCH --job-name=runline-extract
#SBATCH --partition=cpu
#SBATCH --cpus-per-task=4
#SBATCH --mem=32G
#SBATCH --time=12:00:00
#SBATCH --output=slurm/logs/%x-%j.out
set -euo pipefail
: "${EXTRACTS:?pass --export=EXTRACTS=<directory with extract.sh>}"

cd "$SLURM_SUBMIT_DIR"
export PATH="$HOME/.local/osmium/bin:$PATH"
command -v osmium >/dev/null || { echo "osmium-tool not found; see the install note above"; exit 1; }
bash "$EXTRACTS/extract.sh"
