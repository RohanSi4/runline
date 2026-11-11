#!/bin/bash
# sbatch with this cluster's partition and allocation (slurm/clusters/*.env).
# Use it for any job submitted by hand, from the repo root:
#
#   bash slurm/sbatch.sh --export=ALL,AREA=charlottesville-va slurm/precompute.sbatch
#
# The .sbatch files name no partition or account, so plain sbatch needs
# -p (and -A on Rivanna). DRY_RUN=1 prints the command instead.
set -euo pipefail
cd "$(dirname "$0")/.."
# shellcheck source=cluster.sh
source slurm/cluster.sh
mkdir -p slurm/logs
submit "$@"
