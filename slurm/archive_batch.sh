#!/bin/bash
# Archive a merged batch before anything on scratch is purged. Run on a login
# node, from anywhere in the repo, once run_batch.sh's merge has finished:
#
#   bash slurm/archive_batch.sh b1 [laptop destination]
#
# It submits two jobs, so the hashing and packing (tens to hundreds of GB)
# stay off the login node:
#
#   1. runline-manifest: scripts/batch_manifest.py writes
#      $BASE/batches/<batch>/manifest.json (size and sha256 of the plan,
#      every shard, pools.sqlite and the batch's exact tile graphs, plus the
#      plan's cells, each tile's graph hash, pool counts, quick_check and the
#      cell index). It fails, and the pack never runs, if the batch is not
#      servable as it stands; ALLOW_PROBLEMS=1 archives it anyway.
#   2. runline-pack, after the manifest succeeds: one uncompressed tar of
#      those files (paths relative to $BASE; tile graphs and shards are
#      already gzipped), split into PART_SIZE (default 3G) parts in
#      ARCHIVE_DIR (default $BASE/archives/<batch>), with <batch>.parts.sha256
#      and a copy of the manifest beside them.
#
# Then it prints the one rsync that pulls ARCHIVE_DIR to the laptop over the
# ControlMaster connection (README: SSH), and the laptop's verify and stage
# commands. The destination defaults to ~/runline-archive; a Drive-synced
# folder works too.
#
# HERE=1 runs both steps in this shell instead (inside an ijob or salloc, or
# for a small batch). DRY_RUN=1 prints every command and runs none.
# The steps can also be run on their own: --manifest <batch>, --pack <batch>.
# PY overrides .venv/bin/python, SSH_HOST the host in the printed rsync.
set -euo pipefail
cd "$(dirname "$0")/.."
# shellcheck source=cluster.sh
source slurm/cluster.sh
PY=${PY:-.venv/bin/python}
PART_SIZE=${PART_SIZE:-3G}
JOBS=${JOBS:-4}

usage="usage: archive_batch.sh <batch> [destination] | --manifest <batch> | --pack <batch>"
step=all
case "${1:-}" in
--manifest | --pack)
    step=${1#--}
    shift
    ;;
-* | '') echo "$usage" >&2; exit 2 ;;
esac
NAME=${1:?$usage}
DEST=${2:-'~/runline-archive'}
WORK=$BASE/batches/$NAME
ARCHIVE_DIR=${ARCHIVE_DIR:-$BASE/archives/$NAME}
SSH_HOST=${SSH_HOST:-$USER@$LOGIN_HOST}

# Print a command in a dry run, else run it.
run() {
    if [ "${DRY_RUN:-0}" = 1 ]; then
        echo "[dry run] $*" >&2
    else
        "$@"
    fi
}

manifest_step() {
    [ -n "${SLURM_JOB_ID:-}" ] && job_prelude
    run "$PY" scripts/batch_manifest.py "$WORK" --jobs "$JOBS" ${ALLOW_PROBLEMS:+--allow-problems}
}

pack_step() {
    [ -n "${SLURM_JOB_ID:-}" ] && job_prelude
    local prefix=$ARCHIVE_DIR/$NAME.tar.
    if [ "${DRY_RUN:-0}" = 1 ]; then
        echo "[dry run] mkdir -p $ARCHIVE_DIR" >&2
        echo "[dry run] $PY scripts/batch_manifest.py $WORK --list | tar -C $BASE -cf - -T - | split -a 3 -b $PART_SIZE - $prefix" >&2
        echo "[dry run] (cd $ARCHIVE_DIR && sha256sum $NAME.tar.* > $NAME.parts.sha256)" >&2
        echo "[dry run] cp $WORK/manifest.json $ARCHIVE_DIR/$NAME-manifest.json" >&2
        return 0
    fi
    [ -f "$WORK/manifest.json" ] || { echo "no $WORK/manifest.json; run --manifest $NAME first" >&2; exit 1; }
    mkdir -p "$ARCHIVE_DIR"
    rm -f "$prefix"* "$ARCHIVE_DIR/$NAME.parts.sha256"
    # The parts take as much room as the batch; refuse rather than fill the disk.
    "$PY" -c "
import json, shutil, sys
need = json.load(open(sys.argv[1]))['total_bytes']
free = shutil.disk_usage(sys.argv[2]).free
if free < need * 1.05:
    sys.exit(f'{sys.argv[2]} has {free / 2**30:.1f} GB free, the parts need {need / 2**30:.1f} GB')
" "$WORK/manifest.json" "$ARCHIVE_DIR"
    "$PY" scripts/batch_manifest.py "$WORK" --list | tar -C "$BASE" -cf - -T - | split -a 3 -b "$PART_SIZE" - "$prefix"
    local sum="sha256sum"
    command -v sha256sum >/dev/null 2>&1 || sum="shasum -a 256"
    (cd "$ARCHIVE_DIR" && $sum "$NAME".tar.* >"$NAME.parts.sha256")
    cp "$WORK/manifest.json" "$ARCHIVE_DIR/$NAME-manifest.json"
    echo "packed $(ls "$prefix"* | wc -l | tr -d ' ') parts, $(du -sh "$ARCHIVE_DIR" | cut -f1), in $ARCHIVE_DIR"
}

laptop_steps() {
    local here="$DEST/$NAME" ssh_opts="ssh -o ControlMaster=auto -o ControlPath=~/.ssh/cm-%r@%h:%p -o ControlPersist=8h"
    cat <<EOF

When the pack is done, on the laptop. One rsync pulls every part over one
connection (it reuses an open ControlMaster connection, or becomes one); do
not loop ssh or rsync per file. Rerun the same command to resume.

  rsync -a --partial --progress -e "$ssh_opts" \\
      $SSH_HOST:$ARCHIVE_DIR/ $here/
  (cd $here && shasum -a 256 -c $NAME.parts.sha256)
  mkdir -p $here/unpacked && cat $here/$NAME.tar.* | tar -xf - -C $here/unpacked
  .venv/bin/python scripts/batch_manifest.py --verify $here/unpacked/batches/$NAME
  .venv/bin/python scripts/stage_metro_run.py $here/unpacked/batches/$NAME --metros-dir metros --skip-verify

Unpack on a local disk, not inside a Drive-synced folder. Only once
--verify passes on the laptop (and on a second copy, if one is kept) delete
$WORK, $ARCHIVE_DIR and the batch's tile graphs from $BASE.
EOF
}

case "$step" in
manifest) manifest_step; exit 0 ;;
pack) pack_step; exit 0 ;;
esac

if [ "${DRY_RUN:-0}" != 1 ] && [ ! -f "$WORK/pools/pools.sqlite" ]; then
    echo "no $WORK/pools/pools.sqlite; wait for the batch's merge (squeue -n runline-merge)" >&2
    exit 1
fi
echo "archiving batch $NAME from $WORK to $ARCHIVE_DIR"
if [ "${HERE:-0}" = 1 ]; then
    manifest_step
    pack_step
else
    [ "${DRY_RUN:-0}" = 1 ] || mkdir -p slurm/logs
    manifest=$(submit --parsable --job-name=runline-manifest --cpus-per-task="$JOBS" --mem=4G --time=08:00:00 \
        --output=slurm/logs/%x-%j.out --wrap="bash slurm/archive_batch.sh --manifest $NAME")
    pack=$(submit --parsable --dependency=afterok:"$manifest" --job-name=runline-pack --cpus-per-task=1 \
        --mem=2G --time=12:00:00 --output=slurm/logs/%x-%j.out --wrap="bash slurm/archive_batch.sh --pack $NAME")
    echo "manifest: $manifest  pack: $pack (runs when the manifest succeeds; see slurm/logs/)"
fi
laptop_steps
