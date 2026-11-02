#!/bin/bash
# One-time setup on the UVA CS cluster. Run on a portal node, not in a job:
#
#   ssh vaq5gp@portal.cs.virginia.edu
#   cd ~/runline && bash slurm/setup.sh
#
# The repo gets there from a laptop with:
#
#   rsync -av --exclude .venv --exclude cache --exclude pools --exclude build-cache \
#       ~/Desktop/RunLine/runline/ vaq5gp@portal.cs.virginia.edu:~/runline/
set -euo pipefail
cd "$(dirname "$0")/.."

module purge
module load python

python3 - <<'EOF'
import sys
if sys.version_info < (3, 11):
    sys.exit(f"Python {sys.version.split()[0]} is too old; runline needs 3.11+. Try `module avail python`.")
EOF

mkdir -p slurm/logs
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
# The precompute only reads shipped graphs, so the runtime set is enough;
# osmnx is needed only to build new areas.
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m pip install -e . --no-deps

for slug in $(.venv/bin/python -c "import json; print(' '.join(a['slug'] for a in json.load(open('graphs/manifest.json'))['areas']))"); do
    .venv/bin/python scripts/precompute_pools.py --area "$slug" --list --shard-count 400
done
echo "setup done"
