#!/bin/bash
# Merge an area's pool shards into pools/<area>.sqlite once the array is done.
#
#   sbatch --export=AREA=charlottesville-va slurm/merge.sbatch
#
# Or chain it so it starts only if every array task succeeded:
#
#   jid=$(sbatch --parsable --export=AREA=charlottesville-va slurm/precompute.sbatch)
#   sbatch --dependency=afterok:$jid --export=AREA=charlottesville-va slurm/merge.sbatch
#SBATCH --job-name=runline-merge
#SBATCH --partition=cpu
#SBATCH --cpus-per-task=1
#SBATCH --mem=4G
#SBATCH --time=00:30:00
#SBATCH --output=slurm/logs/%x-%j.out
set -euo pipefail
: "${AREA:?pass --export=AREA=<slug>}"

cd "$SLURM_SUBMIT_DIR"
# .venv runs uv's standalone Python 3.12 (see setup.sh), so no modules.
.venv/bin/python scripts/merge_pools.py --area "$AREA" --expect-shards "${SHARDS:-400}"
