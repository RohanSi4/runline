#!/usr/bin/env bash
# Back pool files up to Azure Blob Storage, from the machine that has them.
#
#   az login
#   deploy/backup_pools.sh <storage-account> pools/charlottesville-va.sqlite
#
# Runs on the laptop, not the VM, so the VM holds no cloud credentials: a
# pool file never changes once built, so the copy you upload from is the
# one to back up. Each file is stored once under <area>/<sha256 prefix>.sqlite
# with its graph hash and build commit as metadata, so a backup is immutable
# and you can tell which graph it needs before restoring it. deploy/README.md
# has the one-time storage account setup and the restore command.
set -euo pipefail

if [[ $# -lt 2 ]]; then
  echo "usage: $0 <storage-account> pools/<area>.sqlite [more.sqlite ...]" >&2
  exit 2
fi
ACCOUNT="$1"
shift
CONTAINER="${CONTAINER:-pools}"
AZ=(az storage --only-show-errors)
AUTH=(--account-name "$ACCOUNT" --auth-mode login)

sha256() {
  if command -v sha256sum >/dev/null; then sha256sum "$1"; else shasum -a 256 "$1"; fi | cut -d' ' -f1
}
meta() {
  sqlite3 "$1" "SELECT value FROM meta WHERE key = '$2'" | tr -d '"'
}

"${AZ[@]}" container create "${AUTH[@]}" --name "$CONTAINER" >/dev/null

for file in "$@"; do
  area=$(basename "$file" .sqlite)
  blob="$area/$(sha256 "$file" | cut -c1-16).sqlite"
  exists=$("${AZ[@]}" blob exists "${AUTH[@]}" --container-name "$CONTAINER" --name "$blob" --query exists -o tsv)
  if [[ "$exists" == "true" ]]; then
    echo "$blob already backed up"
    continue
  fi
  "${AZ[@]}" blob upload "${AUTH[@]}" --container-name "$CONTAINER" --name "$blob" --file "$file" \
    --metadata "graph=$(meta "$file" graph)" "commit=$(meta "$file" commit)" "format=$(meta "$file" format)" \
    --overwrite false >/dev/null
  echo "uploaded $blob"
done
"${AZ[@]}" blob list "${AUTH[@]}" --container-name "$CONTAINER" \
  --query "[].{name: name, bytes: properties.contentLength, modified: properties.lastModified}" -o table
