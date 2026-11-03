#!/bin/bash
# Route-pool precompute as a SLURM array: one shard per task, one core each.
#
#   sbatch --export=AREA=charlottesville-va slurm/precompute.sbatch
#
# Shards that already exist are skipped, so resubmitting the same command
# reruns only failed or timed-out tasks. Watch with `squeue -u $USER` and the
# logs in slurm/logs/. When every task is done, run slurm/merge.sbatch.
#
# Charlottesville is 4,364 cells at 200 m. On the cluster a cell took about
# 80 s for three surfaces at 15 targets (format 1); 12 targets is about 25%
# less, so a shard of 11 cells takes about 11 minutes.
#SBATCH --job-name=runline-pools
# cpu and gpu each allow 400 cores per user, and nothing here needs a GPU, so
# tasks start in whichever partition has a free core: up to 800 at once.
#SBATCH --partition=cpu,gpu
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
# One core per task: keep numpy/scipy from starting extra threads.
export OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1

.venv/bin/python scripts/precompute_pools.py \
    --area "$AREA" \
    --shard-index "$SLURM_ARRAY_TASK_ID" \
    --shard-count "$SHARDS"
