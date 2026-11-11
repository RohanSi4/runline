#!/bin/bash
# Route-pool precompute as a SLURM array: one shard per task, one core each.
#
#   bash slurm/sbatch.sh --export=ALL,AREA=charlottesville-va slurm/precompute.sbatch
#
# Shards that already exist are skipped, so resubmitting the same command
# reruns only failed or timed-out tasks. Watch with `squeue -u $USER` and the
# logs in slurm/logs/. When every task is done, run slurm/merge.sbatch.
#
# Charlottesville is 4,364 cells at 200 m. On the cluster a cell took about
# 80 s for three surfaces at 15 targets (format 1); 12 targets is about 25%
# less, so a shard of 11 cells takes about 11 minutes.
#SBATCH --job-name=runline-pools
# No partition or account here: submit through run_batch.sh, submit_tiles.sh
# or sbatch.sh, which add them from slurm/clusters/<cluster>.env.
#SBATCH --array=0-399
#SBATCH --cpus-per-task=1
#SBATCH --mem=3G
#SBATCH --time=02:00:00
#SBATCH --output=slurm/logs/%x-%A_%a.out
set -euo pipefail
: "${AREA:?pass --export=AREA=<slug>}"
SHARDS=${SHARDS:-400}

cd "$SLURM_SUBMIT_DIR"
# .venv runs uv's standalone Python 3.12 (see setup.sh), so no modules.
# job_prelude also keeps numpy/scipy to one thread, as each task has one core.
# shellcheck source=cluster.sh
source slurm/cluster.sh
job_prelude

.venv/bin/python scripts/precompute_pools.py \
    --area "$AREA" \
    --shard-index "$SLURM_ARRAY_TASK_ID" \
    --shard-count "$SHARDS"
