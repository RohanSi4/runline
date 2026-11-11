# Cluster settings for the slurm/ scripts. Source it from the repo root:
#
#   source slurm/cluster.sh
#
# It loads slurm/clusters/$CLUSTER.env (cs or rivanna) and defines
# `submit`, a drop-in for sbatch that adds the cluster's partition and
# allocation. CLUSTER is picked in this order: the CLUSTER variable, then the
# host name (*.hpc.virginia.edu is Rivanna), then the `allocations` command
# that only UVA RC clusters have, then cs.
#
# DRY_RUN=1 makes `submit` print each sbatch command to stderr instead of
# running it, and hand back a placeholder job id so later steps still chain.

if [ -z "${CLUSTER:-}" ]; then
    case "$(hostname -f 2>/dev/null || hostname)" in
        *.hpc.virginia.edu) CLUSTER=rivanna ;;
        *.cs.virginia.edu) CLUSTER=cs ;;
        *) if command -v allocations >/dev/null 2>&1; then CLUSTER=rivanna; else CLUSTER=cs; fi ;;
    esac
fi
_cluster_env="$(dirname "${BASH_SOURCE[0]}")/clusters/$CLUSTER.env"
if [ ! -f "$_cluster_env" ]; then
    echo "no cluster config $_cluster_env (CLUSTER=$CLUSTER)" >&2
    return 1 2>/dev/null || exit 1
fi
# shellcheck source=clusters/cs.env
source "$_cluster_env"
# Jobs are submitted with --export=ALL, so these reach them.
export CLUSTER MODULE_PURGE OSMIUM_PREFIX

# Where `submit` keeps its placeholder job count in a dry run, shared with
# child scripts such as submit_tiles.sh.
if [ "${DRY_RUN:-0}" = 1 ] && [ -z "${DRY_RUN_SEQ:-}" ]; then
    DRY_RUN_SEQ=$(mktemp "${TMPDIR:-/tmp}/runline-dry.XXXXXX")
    export DRY_RUN DRY_RUN_SEQ
fi

# sbatch with the cluster's --partition (unless one is passed) and --account.
submit() {
    local extra=() arg name=job partition=1
    for arg in "$@"; do
        case "$arg" in
            -p | --partition | --partition=*) partition=0 ;;
        esac
    done
    [ "$partition" = 1 ] && extra+=(--partition="$PARTITION")
    if [ -n "${ACCOUNT:-}" ]; then
        extra+=(--account="$ACCOUNT")
    elif [ "${ACCOUNT_REQUIRED:-0}" = 1 ]; then
        if [ "${DRY_RUN:-0}" = 1 ]; then
            extra+=(--account=ALLOCATION)
        else
            echo "$CLUSTER_LABEL needs an allocation: export ACCOUNT=<name from the allocations command>" >&2
            return 1
        fi
    fi
    if [ "${DRY_RUN:-0}" = 1 ]; then
        for arg in "$@"; do
            case "$arg" in
                *.sbatch) name=$(basename "$arg" .sbatch) ;;
                --job-name=*) name=${arg#--job-name=} ;;
            esac
        done
        local seq line=sbatch
        seq=$(($(wc -l <"$DRY_RUN_SEQ") + 1))
        echo >>"$DRY_RUN_SEQ"
        for arg in ${extra[@]+"${extra[@]}"} "$@"; do
            # Show the cluster's paths, not the stand-in directory.
            if [ -n "${DRY_RUN_TMP_BASE:-}" ]; then arg=${arg//"$DRY_RUN_TMP_BASE"/$DRY_RUN_SHOWN_BASE}; fi
            case "$arg" in
                *[!A-Za-z0-9_./:=,%@+-]*) line+=" '$arg'" ;;
                *) line+=" $arg" ;;
            esac
        done
        echo "[dry run] $line" >&2
        echo "DRY${seq}-${name}"
        return 0
    fi
    sbatch ${extra[@]+"${extra[@]}"} "$@"
}

# For job scripts, after cd "$SLURM_SUBMIT_DIR": one core per task, and on
# clusters with Lmod no modules (so no module Python) in the way of .venv.
job_prelude() {
    if [ "${MODULE_PURGE:-0}" = 1 ] && command -v module >/dev/null 2>&1; then
        # Lmod reads unset variables, which set -u would turn into errors.
        set +u
        module purge
        set -u
    fi
    unset PYTHONPATH PYTHONHOME
    export OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1
}

# A dry run off the cluster cannot write to /scratch or /bigtemp; work in a
# temporary directory instead so the planning steps still run for real.
dry_run_base() {
    if [ "${DRY_RUN:-0}" = 1 ] && ! mkdir -p "$BASE" 2>/dev/null; then
        DRY_RUN_SHOWN_BASE=$BASE
        BASE=$(mktemp -d "${TMPDIR:-/tmp}/runline-base.XXXXXX")
        DRY_RUN_TMP_BASE=$BASE
        US_PBF=$BASE/us-latest.osm.pbf
        export DRY_RUN_SHOWN_BASE DRY_RUN_TMP_BASE
        echo "[dry run] $DRY_RUN_SHOWN_BASE is not writable here; planning in $BASE," \
            "shown below as $DRY_RUN_SHOWN_BASE" >&2
    fi
}

if [ "${DRY_RUN:-0}" = 1 ]; then
    echo "cluster: $CLUSTER ($CLUSTER_LABEL), base $BASE, dry run" >&2
else
    echo "cluster: $CLUSTER ($CLUSTER_LABEL), base $BASE" >&2
fi
