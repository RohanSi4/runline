#!/usr/bin/env bash
# Set up, or update, RunLine on an Ubuntu 24.04 LTS VM. Run it as root:
#
#   sudo bash provision.sh --domain runline-xyz.eastus.cloudapp.azure.com
#   sudo bash provision.sh --domain a.example.com,b.example.com --ref host-pools-vm
#
# Safe to rerun: each step checks before it changes anything, and a rerun is
# how the app is updated (it checks out --ref, reinstalls requirements.txt and
# restarts). deploy/README.md is the runbook around it.
#
#   --domain   names Caddy serves and gets certificates for, comma-separated
#   --ref      git branch, tag or commit to run (default main)
#   --repo     git URL to clone (default the public GitHub repo)
#   --workers  uvicorn workers (default: from RAM, see below)
#   --swap-gb  swap file size when none exists (default 2; 0 leaves swap alone)
#   --uploader login that copies pool files up (default the sudo user)
set -euo pipefail

DOMAIN=""
REF="main"
REPO="https://github.com/RohanSi4/runline.git"
WORKERS=""
SWAP_GB=2
UPLOADER="${SUDO_USER:-}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --domain) DOMAIN="$2"; shift 2 ;;
    --ref) REF="$2"; shift 2 ;;
    --repo) REPO="$2"; shift 2 ;;
    --workers) WORKERS="$2"; shift 2 ;;
    --swap-gb) SWAP_GB="$2"; shift 2 ;;
    --uploader) UPLOADER="$2"; shift 2 ;;
    *) echo "unknown option $1" >&2; exit 2 ;;
  esac
done

if [[ $EUID -ne 0 ]]; then
  echo "run as root: sudo bash $0 ..." >&2
  exit 1
fi
if [[ -z "$DOMAIN" ]]; then
  echo "pass --domain (the VM's DNS name, or your own domain once it points here)" >&2
  exit 2
fi
if [[ -z "$UPLOADER" ]] || ! id "$UPLOADER" >/dev/null 2>&1; then
  echo "pass --uploader with the login that will copy pool files up" >&2
  exit 2
fi

APP=/opt/runline/app
VENV=/opt/runline/venv
POOLS=/srv/runline/pools

step() { printf '\n== %s\n' "$*"; }

step "packages"
export DEBIAN_FRONTEND=noninteractive
# Ubuntu's own caddy package (universe) is enough: automatic HTTPS has been in
# Caddy since 2.0, and it avoids adding a third-party apt repository.
packages=(git python3 python3-venv rsync sqlite3 caddy curl)
missing=()
for package in "${packages[@]}"; do
  dpkg-query -W -f='${Status}' "$package" 2>/dev/null | grep -q "install ok installed" || missing+=("$package")
done
if [[ ${#missing[@]} -gt 0 ]]; then
  apt-get update -q
  apt-get install -y -q "${missing[@]}"
fi
# numpy and scipy as pinned need Python 3.12; Ubuntu 22.04's 3.10 cannot
# install them.
python3 -c 'import sys; sys.exit(sys.version_info < (3, 12))' || {
  echo "need Python 3.12 or newer (Ubuntu 24.04 LTS); found $(python3 --version)" >&2
  exit 1
}

step "swap"
# The B1s has 1 GB. One worker peaked at 775 MB in testing, so swap is what
# turns a burst of drive-radius requests into a slow answer instead of an
# out-of-memory kill.
if [[ "$SWAP_GB" == 0 ]]; then
  echo "left alone (--swap-gb 0)"
else
  if ! swapon --show=NAME --noheadings | grep -q .; then
    if [[ ! -f /swapfile ]]; then
      fallocate -l "${SWAP_GB}G" /swapfile
      chmod 600 /swapfile
      mkswap /swapfile
    fi
    swapon /swapfile
  fi
  grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
  # Prefer dropping file cache to swapping out the graph a worker searches.
  echo 'vm.swappiness=10' > /etc/sysctl.d/90-runline.conf
  sysctl -q -p /etc/sysctl.d/90-runline.conf
fi

step "users and directories"
id runline >/dev/null 2>&1 || useradd --system --home-dir /opt/runline --shell /usr/sbin/nologin runline
install -d -m 755 /opt/runline /srv/runline /etc/runline
# The uploader writes pool files without sudo; the service only reads them.
install -d -m 755 -o "$UPLOADER" -g "$UPLOADER" "$POOLS" "$POOLS/.incoming"

step "code at $REF"
if [[ ! -d "$APP/.git" ]]; then
  git clone --quiet "$REPO" "$APP"
fi
git -C "$APP" fetch --quiet --tags origin
# A branch name means its latest commit on origin, not a stale local copy.
if git -C "$APP" rev-parse --verify --quiet "origin/$REF^{commit}" >/dev/null; then
  git -C "$APP" checkout --quiet --detach "origin/$REF"
else
  git -C "$APP" checkout --quiet --detach "$REF"
fi
echo "running $(git -C "$APP" log -1 --format='%h %s')"

step "virtualenv"
[[ -x "$VENV/bin/python" ]] || python3 -m venv "$VENV"
"$VENV/bin/pip" install --quiet --upgrade pip
# Exact pins: the shipped graph pickles only load under the versions that
# built them (CLAUDE.md).
"$VENV/bin/pip" install --quiet -r "$APP/requirements.txt"
# The service cannot write the checkout, so compile it here; otherwise every
# worker recompiles src/ on each start.
"$VENV/bin/python" -m compileall -q "$APP/src"

step "service"
total_mb=$(awk '/^MemTotal:/ {print int($2 / 1024)}' /proc/meminfo)
cpus=$(nproc)
if [[ -z "$WORKERS" ]]; then
  # About 800 MB per worker at its measured worst, after ~500 MB for the OS
  # and Caddy, and no more workers than CPUs: live planning is CPU-bound, so
  # a second worker on one vCPU only doubles the memory.
  WORKERS=$(( (total_mb - 500) / 800 ))
  if (( WORKERS > cpus )); then WORKERS=$cpus; fi
  if (( WORKERS < 1 )); then WORKERS=1; fi
fi
cat > /etc/runline/runline.env <<EOF
# Written by deploy/provision.sh; rerun it with --workers to change this.
RUNLINE_WORKERS=$WORKERS
EOF
install -d -m 755 /etc/systemd/system/runline.service.d
# Above MemoryHigh the kernel reclaims and swaps the service's own pages, so
# sshd and Caddy keep their memory; MemoryMax is the hard stop before the
# whole VM would thrash. Scaled to this VM's RAM so resizing needs no edits.
cat > /etc/systemd/system/runline.service.d/memory.conf <<EOF
# Written by deploy/provision.sh from this VM's ${total_mb} MB of RAM.
[Service]
MemoryHigh=$(( total_mb - 350 ))M
MemoryMax=$(( total_mb - 150 ))M
EOF
install -m 644 "$APP/deploy/runline.service" /etc/systemd/system/runline.service
systemctl daemon-reload
systemctl enable --quiet runline
systemctl restart runline
echo "uvicorn with $WORKERS worker(s) on $cpus CPU(s), ${total_mb} MB RAM"

step "caddy for $DOMAIN"
sites=$(echo "$DOMAIN" | sed 's/,/, /g')
sed "s|^RUNLINE_SITES {|$sites {|" "$APP/deploy/Caddyfile" > /etc/caddy/Caddyfile.new
# Quiet when the config is good; Caddy logs a page of JSON either way.
if ! output=$(caddy validate --adapter caddyfile --config /etc/caddy/Caddyfile.new 2>&1); then
  echo "$output" >&2
  exit 1
fi
mv /etc/caddy/Caddyfile.new /etc/caddy/Caddyfile
systemctl enable --quiet caddy
systemctl reload-or-restart caddy

step "health"
for _ in $(seq 1 30); do
  if curl -fsS http://127.0.0.1:8000/api/health >/dev/null 2>&1; then
    echo "uvicorn is answering on 127.0.0.1:8000"
    ls -l "$POOLS"/*.sqlite 2>/dev/null || echo "no pool files in $POOLS yet: every request plans live (deploy/upload_pools.sh)"
    exit 0
  fi
  sleep 1
done
echo "uvicorn did not answer; see: journalctl -u runline -n 50" >&2
exit 1
