#!/bin/bash
# Build metro tile walk graphs from the per-tile extracts, one shard per task.
#
#   bash slurm/sbatch.sh --export=ALL,EXTRACTS=$BASE/tiles,GRAPHS=$BASE/tile-graphs \
#       slurm/build_tiles.sbatch
#
# The array must match SHARDS (800 by default). METRO=<slug> builds one metro's
# tiles only (a pilot); size the array to its tile count.
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
# No partition or account here: submit through run_batch.sh, submit_tiles.sh
# or sbatch.sh, which add them from slurm/clusters/<cluster>.env.
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
# shellcheck source=cluster.sh
source slurm/cluster.sh
job_prelude
# DEM tiles are shared by every task; keep them next to the graphs.
.venv/bin/python scripts/build_tile_graphs.py build \
    --extracts "$EXTRACTS" --output "$GRAPHS" --dem-dir "$GRAPHS/dem" \
    --shard-index "$SLURM_ARRAY_TASK_ID" --shard-count "$SHARDS" \
    ${METRO:+--metro $METRO}
