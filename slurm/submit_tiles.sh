#!/bin/bash
# Submit a metro precompute plan as several arrays plus a merge that waits
# for all of them. Run on a login node, with BASE as in slurm/clusters/*.env:
#
#   python scripts/plan_tile_pools.py --output $BASE/us-pools
#   bash slurm/submit_tiles.sh $BASE/us-pools/plan.json.gz \
#       $BASE/tile-graphs $BASE/us-pools
#
# Cluster defaults come from slurm/clusters/<cluster>.env (see cluster.sh).
# TASKS (default MAX_TASKS, 4000) is how many tasks share the plan, ARRAY
# (MAX_ARRAY, 1000) the size of each array, CONCURRENT how many run at once
# per array, PARTITION where they run. The cluster's per-user core limit caps
# the total. AFTER (e.g. afterany:<tile build job>) holds the arrays until that
# job finishes. MEM (e.g. 8G) overrides precompute_tiles.sbatch's memory per
# task and PRECOMPUTE_TIME its time limit. DRY_RUN=1 prints the sbatch lines.
set -euo pipefail
PLAN=${1:?plan.json.gz} GRAPHS=${2:?tile graph directory} OUT=${3:?output directory}
cd "$(dirname "$0")/.."
# shellcheck source=cluster.sh
source slurm/cluster.sh
TASKS=${TASKS:-$MAX_TASKS} ARRAY=${ARRAY:-$MAX_ARRAY}
AFTER=${AFTER:-}
mkdir -p slurm/logs "$OUT"

jobs=()
for ((offset = 0; offset < TASKS; offset += ARRAY)); do
    last=$((offset + ARRAY < TASKS ? ARRAY - 1 : TASKS - offset - 1))
    jobs+=("$(submit --parsable --array="0-${last}%${CONCURRENT}" \
        ${AFTER:+--dependency=$AFTER} ${MEM:+--mem=$MEM} ${PRECOMPUTE_TIME:+--time=$PRECOMPUTE_TIME} \
        --export="ALL,PLAN=$PLAN,GRAPHS=$GRAPHS,OUT=$OUT,TASKS=$TASKS,TASK_OFFSET=$offset" \
        slurm/precompute_tiles.sbatch)")
done
dependency=$(IFS=:; echo "afterok:${jobs[*]}")
merge=$(submit --parsable --dependency="$dependency" --mem=16G --time=12:00:00 \
    --wrap=".venv/bin/python scripts/merge_pools.py --plan $PLAN --input $OUT" \
    --job-name=runline-merge --output=slurm/logs/%x-%j.out)
echo "arrays: ${jobs[*]}"
echo "merge: $merge (runs when every array succeeds)"
