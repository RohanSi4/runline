#!/bin/bash
# Submit a metro precompute plan as several arrays plus a merge that waits
# for all of them. Run on portal:
#
#   python scripts/plan_tile_pools.py --output /bigtemp/$USER/us-pools
#   bash slurm/submit_tiles.sh /bigtemp/$USER/us-pools/plan.json.gz \
#       /bigtemp/$USER/tile-graphs /bigtemp/$USER/us-pools
#
# TASKS (default 4000) is how many tasks share the plan, ARRAY (default 1000)
# the size of each array, CONCURRENT (default 400) how many run at once per
# array. The cpu partition allows 400 cores per user in total, so the arrays
# queue behind each other.
set -euo pipefail
PLAN=${1:?plan.json.gz} GRAPHS=${2:?tile graph directory} OUT=${3:?output directory}
TASKS=${TASKS:-4000} ARRAY=${ARRAY:-1000} CONCURRENT=${CONCURRENT:-400}
cd "$(dirname "$0")/.."
mkdir -p slurm/logs "$OUT"

jobs=()
for ((offset = 0; offset < TASKS; offset += ARRAY)); do
    last=$((offset + ARRAY < TASKS ? ARRAY - 1 : TASKS - offset - 1))
    jobs+=("$(sbatch --parsable --array="0-${last}%${CONCURRENT}" \
        --export="ALL,PLAN=$PLAN,GRAPHS=$GRAPHS,OUT=$OUT,TASKS=$TASKS,TASK_OFFSET=$offset" \
        slurm/precompute_tiles.sbatch)")
done
dependency=$(IFS=:; echo "afterok:${jobs[*]}")
merge=$(sbatch --parsable --dependency="$dependency" --mem=16G --time=12:00:00 \
    --wrap=".venv/bin/python scripts/merge_pools.py --plan $PLAN --input $OUT" \
    --job-name=runline-merge --output=slurm/logs/%x-%j.out)
echo "arrays: ${jobs[*]}"
echo "merge: $merge (runs when every array succeeds)"
