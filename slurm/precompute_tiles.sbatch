#!/bin/bash
# Metro route-pool precompute: a block of plan units per task, each on its
# own tile graph. Submit with slurm/submit_tiles.sh, which splits the plan
# over several arrays (MaxArraySize is often 1001) by setting TASK_OFFSET.
#
# Finished units are skipped, so resubmitting reruns only what failed.
# Memory follows the largest tile graph (see build_tile_graphs.py summary):
# Charlottesville's 40k nodes peaked at 453 MB, and the DC pilot's largest
# task at 2.1 GB. Asking for less lets more tasks fit on each node. If tasks
# die with OUT_OF_MEMORY, resubmit with a larger --mem; finished units skip.
#SBATCH --job-name=runline-tile-pools
# No partition or account here: submit through run_batch.sh, submit_tiles.sh
# or sbatch.sh, which add them from slurm/clusters/<cluster>.env.
#SBATCH --cpus-per-task=1
#SBATCH --mem=4G
#SBATCH --time=2-00:00:00
#SBATCH --output=slurm/logs/%x-%A_%a.out
set -euo pipefail
: "${PLAN:?pass --export=PLAN=...,GRAPHS=...,OUT=...,TASKS=...,TASK_OFFSET=...}"
: "${GRAPHS:?}" "${OUT:?}" "${TASKS:?}" "${TASK_OFFSET:?}"

cd "$SLURM_SUBMIT_DIR"
# shellcheck source=cluster.sh
source slurm/cluster.sh
job_prelude
# LIMIT_CELLS (run_batch.sh --smoke) computes only the first cells of each unit.
.venv/bin/python scripts/precompute_pools.py \
    --plan "$PLAN" --tile-graphs "$GRAPHS" --output "$OUT" \
    --shard-index "$((TASK_OFFSET + SLURM_ARRAY_TASK_ID))" --shard-count "$TASKS" \
    ${LIMIT_CELLS:+--limit-cells "$LIMIT_CELLS"}
