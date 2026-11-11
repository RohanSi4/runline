#!/bin/bash
# Merge an area's pool shards into pools/<area>.sqlite once the array is done.
#
#   bash slurm/sbatch.sh --export=ALL,AREA=charlottesville-va slurm/merge.sbatch
#
# Or chain it so it starts only if every array task succeeded:
#
#   jid=$(bash slurm/sbatch.sh --parsable --export=ALL,AREA=charlottesville-va slurm/precompute.sbatch)
#   bash slurm/sbatch.sh --dependency=afterok:$jid --export=ALL,AREA=charlottesville-va slurm/merge.sbatch
#SBATCH --job-name=runline-merge
# No partition or account here: submit through run_batch.sh, submit_tiles.sh
# or sbatch.sh, which add them from slurm/clusters/<cluster>.env.
#SBATCH --cpus-per-task=1
#SBATCH --mem=4G
#SBATCH --time=00:30:00
#SBATCH --output=slurm/logs/%x-%j.out
set -euo pipefail
: "${AREA:?pass --export=AREA=<slug>}"

cd "$SLURM_SUBMIT_DIR"
# .venv runs uv's standalone Python 3.12 (see setup.sh), so no modules.
# shellcheck source=cluster.sh
source slurm/cluster.sh
job_prelude
.venv/bin/python scripts/merge_pools.py --area "$AREA" --expect-shards "${SHARDS:-400}"
