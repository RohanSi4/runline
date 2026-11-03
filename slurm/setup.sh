#!/bin/bash
# One-time setup on the UVA CS cluster. Run on a portal node, not in a job:
#
#   ssh vaq5gp@portal.cs.virginia.edu
#   cd ~/runline && bash slurm/setup.sh
#   cd ~/runline && BUILD=1 bash slurm/setup.sh   # also tile graph builds
#
# The repo gets there from a laptop with:
#
#   rsync -av --exclude .venv --exclude cache --exclude pools --exclude build-cache \
#       ~/Desktop/RunLine/runline/ vaq5gp@portal.cs.virginia.edu:~/runline/
#
# Python comes from uv rather than the cluster's modules: on portal09 the
# python/3.12.3 module loads, but the directory it points at does not exist,
# and Ubuntu's own python3 is 3.10. uv installs a standalone 3.12 under the
# home directory, which every compute node mounts.
set -euo pipefail
cd "$(dirname "$0")/.."

UV="$HOME/.local/bin/uv"
if [ ! -x "$UV" ]; then
    curl -LsSf https://astral.sh/uv/install.sh | sh
fi
"$UV" python install 3.12

mkdir -p slurm/logs
rm -rf .venv
"$UV" venv --python 3.12 .venv
.venv/bin/python --version
# The precompute only reads shipped graphs, so the runtime set is enough;
# osmnx is needed only to build new areas.
"$UV" pip install --python .venv/bin/python -r requirements.txt
"$UV" pip install --python .venv/bin/python -e . --no-deps
# BUILD=1 adds osmnx and pyosmium for scripts/build_tile_graphs.py. The
# requirements pins still apply, so graphs it pickles load in the runtime set.
if [ "${BUILD:-0}" = 1 ]; then
    "$UV" pip install --python .venv/bin/python -r requirements.txt -e '.[build]'
fi

for slug in $(.venv/bin/python -c "import json; print(' '.join(a['slug'] for a in json.load(open('graphs/manifest.json'))['areas']))"); do
    .venv/bin/python scripts/precompute_pools.py --area "$slug" --list --shard-count 400
done
echo "setup done"
