#!/bin/bash
# Route-pool precompute as a SLURM array: one shard per task, one core each.
#
#   sbatch --export=AREA=charlottesville-va slurm/precompute.sbatch
#
# Shards that already exist are skipped, so resubmitting the same command
# reruns only failed or timed-out tasks. Watch with `squeue -u $USER` and the
# logs in slurm/logs/. When every task is done, run slurm/merge.sbatch.
#
# Charlottesville is 4,364 cells at 200 m. At about 27s per cell for three
# surfaces and 15 targets, a shard of 11 cells takes about 5 minutes.
#SBATCH --job-name=runline-pools
#SBATCH --partition=cpu
#SBATCH --array=0-399%200
#SBATCH --cpus-per-task=1
#SBATCH --mem=3G
#SBATCH --time=02:00:00
#SBATCH --output=slurm/logs/%x-%A_%a.out
set -euo pipefail
: "${AREA:?pass --export=AREA=<slug>}"
SHARDS=${SHARDS:-400}

cd "$SLURM_SUBMIT_DIR"
module purge
module load python
# One core per task: keep numpy/scipy from starting extra threads.
export OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1

.venv/bin/python scripts/precompute_pools.py \
    --area "$AREA" \
    --shard-index "$SLURM_ARRAY_TASK_ID" \
    --shard-count "$SHARDS"
