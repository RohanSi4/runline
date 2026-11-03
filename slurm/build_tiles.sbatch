#!/bin/bash
# Build metro tile walk graphs from the per-tile extracts, one shard per task.
#
#   sbatch --export=EXTRACTS=/bigtemp/$USER/tiles,GRAPHS=/bigtemp/$USER/tile-graphs \
#       slurm/build_tiles.sbatch
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
#SBATCH --partition=cpu
#SBATCH --array=0-399%200
#SBATCH --cpus-per-task=1
#SBATCH --mem=8G
#SBATCH --time=04:00:00
#SBATCH --output=slurm/logs/%x-%A_%a.out
set -euo pipefail
: "${EXTRACTS:?pass --export=EXTRACTS=<tile extracts>,GRAPHS=<output>}"
: "${GRAPHS:?pass --export=EXTRACTS=<tile extracts>,GRAPHS=<output>}"
SHARDS=${SHARDS:-400}

cd "$SLURM_SUBMIT_DIR"
export OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1
# DEM tiles are shared by every task; keep them next to the graphs.
.venv/bin/python scripts/build_tile_graphs.py build \
    --extracts "$EXTRACTS" --output "$GRAPHS" --dem-dir "$GRAPHS/dem" \
    --shard-index "$SLURM_ARRAY_TASK_ID" --shard-count "$SHARDS"
