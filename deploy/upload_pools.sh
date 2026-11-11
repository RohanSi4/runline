#!/usr/bin/env bash
# Copy pool files from this machine to the VM and swap them in.
#
#   deploy/upload_pools.sh azureuser@runline-xyz.eastus.cloudapp.azure.com \
#       pools/charlottesville-va.sqlite
#
# Run from the repo root. Each file is checked twice before it goes live: here
# against this checkout's graph, and on the VM against the graph the server
# actually runs, since serving silently ignores a pool built from another
# graph and plans everything live. The file lands under .incoming/ and is
# renamed into place, so a request never reads a half-copied file and no
# restart is needed: serving opens the file per request and rechecks it when
# its modification time changes.
set -euo pipefail

if [[ $# -lt 2 ]]; then
  echo "usage: $0 user@host pools/<area>.sqlite [more.sqlite ...]" >&2
  exit 2
fi
HOST="$1"
shift
PYTHON="${PYTHON:-.venv/bin/python}"
REMOTE_POOLS=/srv/runline/pools

sha256() {
  if command -v sha256sum >/dev/null; then sha256sum "$1"; else shasum -a 256 "$1"; fi | cut -d' ' -f1
}

for file in "$@"; do
  name=$(basename "$file")
  echo "== $name"
  "$PYTHON" scripts/pool_smoke.py --pool "$file"
  sum=$(sha256 "$file")
  # --partial lets a dropped connection resume a 100 MB copy.
  rsync --partial --progress "$file" "$HOST:$REMOTE_POOLS/.incoming/$name"
  ssh "$HOST" bash -s -- "$REMOTE_POOLS" "$name" "$sum" <<'REMOTE'
set -euo pipefail
pools="$1"; name="$2"; sum="$3"
cd "$pools"
echo "$sum  .incoming/$name" | sha256sum --check --quiet -
/opt/runline/venv/bin/python /opt/runline/app/scripts/pool_smoke.py --pool ".incoming/$name"
# Without --times rsync stamps the copy with the time it finished, so the
# replaced file always has a new mtime and serving rechecks it.
mv -f ".incoming/$name" "$name"
echo "live at $pools/$name"
REMOTE
done
