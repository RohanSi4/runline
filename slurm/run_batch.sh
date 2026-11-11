#!/bin/bash
# Submit one batch of metros from config/metro-batches.json, start to finish:
# cut its tile extracts, build its tile graphs, plan its cells, then compute
# and merge its pools. Each step waits for the one before. Run on a login
# node, from anywhere in the repo:
#
#   bash slurm/run_batch.sh test
#
# Once per cluster, first: download us-latest.osm.pbf to $BASE and run
#   bash slurm/run_batch.sh --filter
# which keeps only highway ways in $BASE/highways.osm.pbf; batches cut from it.
#
# Then, before the test batch, a smoke test of every step on two tiles of
# one metro (a few core-hours), in $BASE/smoke:
#   bash slurm/run_batch.sh --smoke [metro slug]
#
# After a batch, list what to copy off scratch (relative to $BASE):
#   bash slurm/run_batch.sh --files <batch>
#
# Cluster settings (partition, allocation, BASE, core limits) come from
# slurm/clusters/<cluster>.env; see slurm/cluster.sh for how the cluster is
# picked (CLUSTER=rivanna or CLUSTER=cs to force it). Tile graphs are shared
# across batches in $BASE/tile-graphs; each batch's pools land in
# $BASE/batches/<name>/pools/pools.sqlite. Copy them somewhere permanent
# before the next batch: scratch is volatile. Rerunning a batch skips built
# tiles and finished units. MEM (default 4G) sets the precompute tasks' memory.
#
# DRY_RUN=1 prints every sbatch command instead of submitting it. The
# planning steps still run (they take seconds), so array sizes are real; off
# the cluster they write to a temporary directory. PY overrides .venv/bin/python.
set -euo pipefail
cd "$(dirname "$0")/.."
# shellcheck source=cluster.sh
source slurm/cluster.sh
PY=${PY:-.venv/bin/python}
mkdir -p slurm/logs

batch_metros() {
    $PY -c "
import json, sys
batches = {b['name']: b for b in json.load(open('config/metro-batches.json'))['batches']}
if sys.argv[1] not in batches:
    sys.exit(f'no batch {sys.argv[1]!r}; have {list(batches)}')
print(' '.join(batches[sys.argv[1]]['metros']))" "$1"
}

# Exits unless the prefiltered extract is there (a dry run only warns).
need_highways() {
    [ -f "$BASE/highways.osm.pbf" ] && return 0
    if [ "${DRY_RUN:-0}" = 1 ]; then
        echo "[dry run] no $BASE/highways.osm.pbf; on the cluster run --filter first" >&2
    else
        echo "no $BASE/highways.osm.pbf; run with --filter first"
        exit 1
    fi
}

case "${1:-}" in
--filter)
    dry_run_base
    if [ ! -f "$US_PBF" ]; then
        if [ "${DRY_RUN:-0}" = 1 ]; then
            echo "[dry run] no $US_PBF yet" >&2
        else
            echo "download us-latest.osm.pbf to $US_PBF first"
            exit 1
        fi
    fi
    # extract.sbatch without an array runs <EXTRACTS>/filter.sh.
    echo "osmium tags-filter --overwrite -o $BASE/highways.osm.pbf $US_PBF w/highway" \
        >"$BASE/filter.sh"
    echo "filter: $(submit --parsable --export=ALL,EXTRACTS="$BASE" slurm/extract.sbatch)"
    exit 0
    ;;
--files)
    # What to archive: pools are worthless without the exact tile graphs they
    # were built on (serving checks each .pkl.gz's sha256, and a rebuild
    # changes it) and the plan, and the shards rebuild pools.sqlite.
    NAME=${2:?usage: run_batch.sh --files <batch name>}
    read -r -a METROS <<<"$(batch_metros "$NAME")"
    $PY -c "
import gzip, json, os, sys
base, name, metros = sys.argv[1], sys.argv[2], set(sys.argv[3:])
pools = f'batches/{name}/pools'
paths = [f'{pools}/pools.sqlite', f'{pools}/plan.json.gz']
plan = os.path.join(base, pools, 'plan.json.gz')
if os.path.exists(plan):
    with gzip.open(plan, 'rt', encoding='utf-8') as stream:
        paths += [f'{pools}/{unit[\"id\"]}.jsonl.gz' for unit in json.load(stream)['units']]
for metro in json.load(open('config/metros.json'))['metros']:
    if metro['slug'] in metros:
        for tile in metro['tiles']:
            # Empty (all-water) tiles have a .json and no graph.
            paths += [f'tile-graphs/{tile[\"slug\"]}.pkl.gz', f'tile-graphs/{tile[\"slug\"]}.json']
present = [path for path in paths if os.path.exists(os.path.join(base, path))]
missing = [path for path in paths if not path.endswith('.pkl.gz') and path not in present]
if missing:
    print(f'{len(missing)} expected files missing under {base}, e.g. {missing[:3]}', file=sys.stderr)
print('\n'.join(present))" "$BASE" "$NAME" "${METROS[@]}"
    exit 0
    ;;
--smoke)
    dry_run_base
    METRO=${2:-$(batch_metros test | cut -d' ' -f1)}
    need_highways
    WORK=$BASE/smoke
    EXTRACTS=$WORK/tiles GRAPHS=$WORK/tile-graphs OUT=$WORK/pools
    mkdir -p "$EXTRACTS" "$GRAPHS" "$OUT"
    ln -sf "$BASE/highways.osm.pbf" "$EXTRACTS/highways.osm.pbf"
    $PY scripts/build_tile_graphs.py configs --pbf "$US_PBF" --extracts "$EXTRACTS" \
        --metro "$METRO" >/dev/null
    $PY scripts/plan_tile_pools.py --metro "$METRO" --output "$OUT" >/dev/null
    # Keep one unit on each of the first two tiles that own cells, among the
    # tiles the first cut (array task 0) extracts. Prints their positions in
    # the metro's tile list, which are the build array's task ids.
    picked=$($PY -c "
import gzip, json, sys
plan_path, metro, per_cut = sys.argv[1], sys.argv[2], int(sys.argv[3])
tiles = [t['slug'] for m in json.load(open('config/metros.json'))['metros'] if m['slug'] == metro for t in m['tiles']]
with gzip.open(plan_path, 'rt', encoding='utf-8') as stream:
    plan = json.load(stream)
units, picked = [], []
for index, tile in enumerate(tiles[:per_cut]):
    unit = next((u for u in plan['units'] if u['tile'] == tile), None)
    if unit:
        units.append(unit)
        picked.append(index)
    if len(units) == 2:
        break
if not units:
    sys.exit(f'no tile in the first cut of {metro} owns cells')
plan['units'] = units
with gzip.open(plan_path, 'wt', encoding='utf-8') as stream:
    json.dump(plan, stream, separators=(',', ':'))
print(len(tiles), ','.join(map(str, picked)), ' '.join(u['tile'] for u in units))
" "$OUT/plan.json.gz" "$METRO" 14)
    read -r ntiles indices names <<<"$picked"
    echo "smoke test on $METRO: tiles $names"
    extract=$(submit --parsable --array=0-0 --export=ALL,EXTRACTS="$EXTRACTS" slurm/extract.sbatch)
    # Shard i of the metro's tile count builds exactly tile i.
    export EXTRACTS GRAPHS SHARDS=$ntiles METRO
    build=$(submit --parsable --dependency=afterok:"$extract" --array="$indices" --export=ALL \
        slurm/build_tiles.sbatch)
    export LIMIT_CELLS=${LIMIT_CELLS:-3}
    TASKS=2 ARRAY=2 PRECOMPUTE_TIME=${SMOKE_TIME:-01:00:00} AFTER=afterok:$build \
        bash slurm/submit_tiles.sh "$OUT/plan.json.gz" "$GRAPHS" "$OUT"
    echo "extract: $extract  build: $build ($indices)  pools: 2 units, $LIMIT_CELLS cells each"
    echo "check $OUT/pools.sqlite and sacct, then delete $WORK"
    exit 0
    ;;
esac

NAME=${1:?usage: run_batch.sh <batch name from config/metro-batches.json> | --filter | --smoke [metro] | --files <batch>}
read -r -a METROS <<<"$(batch_metros "$NAME")"
dry_run_base
need_highways
# Only the smoke test computes a few cells per unit.
unset LIMIT_CELLS

WORK=$BASE/batches/$NAME
EXTRACTS=$WORK/tiles GRAPHS=$BASE/tile-graphs OUT=$WORK/pools
mkdir -p "$EXTRACTS" "$GRAPHS" "$OUT"
ln -sf "$BASE/highways.osm.pbf" "$EXTRACTS/highways.osm.pbf"
echo "batch $NAME: ${METROS[*]}"

# 1. Tile extracts, about fourteen tiles per task.
$PY scripts/build_tile_graphs.py configs --pbf "$US_PBF" --extracts "$EXTRACTS" \
    --metro "${METROS[@]}" >/dev/null
cuts=$(($(wc -l <"$EXTRACTS/cuts.txt")))
extract=$(submit --parsable --array=0-$((cuts - 1)) --export=ALL,EXTRACTS="$EXTRACTS" slurm/extract.sbatch)

# 2. Tile graphs, one task per tile up to MAX_TILE_SHARDS.
tiles=$($PY -c "
import json, sys
metros = json.load(open('config/metros.json'))['metros']
print(sum(len(m['tiles']) for m in metros if m['slug'] in sys.argv[1:]))" "${METROS[@]}")
shards=$((tiles < MAX_TILE_SHARDS ? tiles : MAX_TILE_SHARDS))
export EXTRACTS GRAPHS SHARDS=$shards METRO="${METROS[*]}"
build=$(submit --parsable --dependency=afterok:"$extract" --array=0-$((shards - 1)) --export=ALL \
    slurm/build_tiles.sbatch)

# 3. Plan this batch's share of the cells, less what pilots that ran on their
# own ("done" in config/metro-batches.json) already computed.
read -r -a DONE <<<"$($PY -c "import json; print(' '.join(json.load(open('config/metro-batches.json')).get('done', [])))")"
$PY scripts/plan_tile_pools.py --batch "${METROS[@]}" ${DONE[@]+--done "${DONE[@]}"} --output "$OUT"
units=$($PY -c "
import gzip, json, sys
print(len(json.load(gzip.open(sys.argv[1], 'rt'))['units']))" "$OUT/plan.json.gz")

# 4. Pools and merge, once every tile graph is built.
tasks=$((units < MAX_TASKS ? units : MAX_TASKS))
TASKS=$tasks AFTER=afterok:$build bash slurm/submit_tiles.sh "$OUT/plan.json.gz" "$GRAPHS" "$OUT"
echo "extract: $extract ($cuts tasks)  build: $build ($shards tasks)  pools: $tasks tasks over $units units"
echo "pools will be at $OUT/pools.sqlite"
