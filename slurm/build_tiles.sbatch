#!/bin/bash
# Build metro tile walk graphs from the per-tile extracts, one shard per task.
#
#   sbatch --export=EXTRACTS=/bigtemp/$USER/tiles,GRAPHS=/bigtemp/$USER/tile-graphs \
#       slurm/build_tiles.sbatch
#
# The array must match SHARDS (800 by default).
#
# Needs BUILD=1 bash slurm/setup.sh (osmnx and pyosmium). Built tiles are
# skipped, so resubmitting reruns only failed ones. When the array is done:
#
#   .venv/bin/python scripts/build_tile_graphs.py summary --output $GRAPHS
#
# Memory: osmnx holds the buffered, unsimplified graph while it builds, which
# for a dense downtown tile can be several GB. Raise --mem and resubmit if
# tasks die with OUT_OF_MEMORY; the summary's largest tiles say how far.
#SBATCH --job-name=runline-tiles
# cpu and gpu each allow 400 cores per user, and nothing here needs a GPU, so
# tasks start in whichever partition has a free core: up to 800 at once.
#SBATCH --partition=cpu,gpu
#SBATCH --array=0-799
#SBATCH --cpus-per-task=1
#SBATCH --mem=8G
#SBATCH --time=04:00:00
#SBATCH --output=slurm/logs/%x-%A_%a.out
set -euo pipefail
: "${EXTRACTS:?pass --export=EXTRACTS=<tile extracts>,GRAPHS=<output>}"
: "${GRAPHS:?pass --export=EXTRACTS=<tile extracts>,GRAPHS=<output>}"
SHARDS=${SHARDS:-800}

cd "$SLURM_SUBMIT_DIR"
export OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1
# DEM tiles are shared by every task; keep them next to the graphs.
.venv/bin/python scripts/build_tile_graphs.py build \
    --extracts "$EXTRACTS" --output "$GRAPHS" --dem-dir "$GRAPHS/dem" \
    --shard-index "$SLURM_ARRAY_TASK_ID" --shard-count "$SHARDS"
