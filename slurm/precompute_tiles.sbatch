#!/bin/bash
# Metro route-pool precompute: a block of plan units per task, each on its
# own tile graph. Submit with slurm/submit_tiles.sh, which splits the plan
# over several arrays (MaxArraySize is often 1001) by setting TASK_OFFSET.
#
# Finished units are skipped, so resubmitting reruns only what failed.
# Memory follows the largest tile graph (see build_tile_graphs.py summary):
# Charlottesville's 40k nodes peaked at 453 MB, and dense downtown tiles can
# be several times that.
#SBATCH --job-name=runline-tile-pools
# cpu and gpu each allow 400 cores per user, and nothing here needs a GPU, so
# tasks start in whichever partition has a free core: up to 800 at once.
#SBATCH --partition=cpu,gpu
#SBATCH --cpus-per-task=1
#SBATCH --mem=8G
#SBATCH --time=2-00:00:00
#SBATCH --output=slurm/logs/%x-%A_%a.out
set -euo pipefail
: "${PLAN:?pass --export=PLAN=...,GRAPHS=...,OUT=...,TASKS=...,TASK_OFFSET=...}"
: "${GRAPHS:?}" "${OUT:?}" "${TASKS:?}" "${TASK_OFFSET:?}"

cd "$SLURM_SUBMIT_DIR"
export OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1
.venv/bin/python scripts/precompute_pools.py \
    --plan "$PLAN" --tile-graphs "$GRAPHS" --output "$OUT" \
    --shard-index "$((TASK_OFFSET + SLURM_ARRAY_TASK_ID))" --shard-count "$TASKS"
