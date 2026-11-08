#!/bin/bash
# Submit one batch of metros from config/metro-batches.json, start to finish:
# cut its tile extracts, build its tile graphs, plan its cells, then compute
# and merge its pools. Each step waits for the one before. Run on portal:
#
#   bash slurm/run_batch.sh test
#
# Once per cluster, first: download us-latest.osm.pbf to $BASE and run
#   bash slurm/run_batch.sh --filter
# which keeps only highway ways in $BASE/highways.osm.pbf; batches cut from it.
#
# BASE (default /bigtemp/$USER) holds everything. Tile graphs are shared
# across batches in $BASE/tile-graphs; each batch's pools land in
# $BASE/batches/<name>/pools/pools.sqlite. Copy that somewhere permanent before
# the next batch: /bigtemp is volatile. Rerunning a batch skips built tiles and
# finished units. MEM (default 4G) sets the precompute tasks' memory.
set -euo pipefail
cd "$(dirname "$0")/.."
BASE=${BASE:-/bigtemp/$USER}
PY=.venv/bin/python
mkdir -p slurm/logs

if [ "${1:-}" = "--filter" ]; then
    [ -f "$BASE/us-latest.osm.pbf" ] || { echo "download us-latest.osm.pbf to $BASE first"; exit 1; }
    # extract.sbatch without an array runs <EXTRACTS>/filter.sh.
    echo "osmium tags-filter --overwrite -o $BASE/highways.osm.pbf $BASE/us-latest.osm.pbf w/highway" \
        >"$BASE/filter.sh"
    echo "filter: $(sbatch --parsable --export=ALL,EXTRACTS="$BASE" slurm/extract.sbatch)"
    exit 0
fi

NAME=${1:?usage: run_batch.sh <batch name from config/metro-batches.json> | --filter}
metros=$($PY -c "
import json, sys
batches = {b['name']: b for b in json.load(open('config/metro-batches.json'))['batches']}
if sys.argv[1] not in batches:
    sys.exit(f'no batch {sys.argv[1]!r}; have {list(batches)}')
print(' '.join(batches[sys.argv[1]]['metros']))" "$NAME")
read -r -a METROS <<<"$metros"
[ -f "$BASE/highways.osm.pbf" ] || { echo "no $BASE/highways.osm.pbf; run with --filter first"; exit 1; }

WORK=$BASE/batches/$NAME
EXTRACTS=$WORK/tiles GRAPHS=$BASE/tile-graphs OUT=$WORK/pools
mkdir -p "$EXTRACTS" "$GRAPHS" "$OUT"
ln -sf "$BASE/highways.osm.pbf" "$EXTRACTS/highways.osm.pbf"
echo "batch $NAME: ${METROS[*]}"

# 1. Tile extracts, about fourteen tiles per task.
$PY scripts/build_tile_graphs.py configs --pbf "$BASE/us-latest.osm.pbf" --extracts "$EXTRACTS" \
    --metro "${METROS[@]}" >/dev/null
cuts=$(($(wc -l <"$EXTRACTS/cuts.txt")))
extract=$(sbatch --parsable --array=0-$((cuts - 1)) --export=ALL,EXTRACTS="$EXTRACTS" slurm/extract.sbatch)

# 2. Tile graphs, one task per tile up to 800.
tiles=$($PY -c "
import json, sys
metros = json.load(open('config/metros.json'))['metros']
print(sum(len(m['tiles']) for m in metros if m['slug'] in sys.argv[1:]))" "${METROS[@]}")
shards=$((tiles < 800 ? tiles : 800))
export EXTRACTS GRAPHS SHARDS=$shards METRO="${METROS[*]}"
build=$(sbatch --parsable --dependency=afterok:"$extract" --array=0-$((shards - 1)) --export=ALL \
    slurm/build_tiles.sbatch)

# 3. Plan this batch's share of the cells.
$PY scripts/plan_tile_pools.py --batch "${METROS[@]}" --output "$OUT"
units=$($PY -c "
import gzip, json, sys
print(len(json.load(gzip.open(sys.argv[1], 'rt'))['units']))" "$OUT/plan.json.gz")

# 4. Pools and merge, once every tile graph is built.
tasks=$((units < 4000 ? units : 4000))
TASKS=$tasks AFTER=afterok:$build bash slurm/submit_tiles.sh "$OUT/plan.json.gz" "$GRAPHS" "$OUT"
echo "extract: $extract ($cuts tasks)  build: $build ($shards tasks)  pools: $tasks tasks over $units units"
echo "pools will be at $OUT/pools.sqlite"
