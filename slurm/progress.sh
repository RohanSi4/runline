#!/bin/bash
# One-screen progress of a metro run. Run on a login node, once per check
# (each `watch` refresh is cheap, but keep the SSH session itself to one):
#
#   bash ~/runline/slurm/progress.sh $BASE/batches/<batch>/tiles \
#       $BASE/tile-graphs $BASE/batches/<batch>/pools
#
# BASE is /bigtemp/$USER on the CS cluster and /scratch/$USER on Rivanna.
# Arguments are the extracts, tile graph and pool directories; missing ones
# are shown as not started.
EXTRACTS=${1:?extracts directory} GRAPHS=${2:?tile graph directory} POOLS=${3:?pool directory}
cd "$(dirname "$0")/.."

count() { find "$1" -maxdepth 1 -name "$2" 2>/dev/null | wc -l; }
expected=$(grep -ho '"output"' "$EXTRACTS"/extract-*.json 2>/dev/null | wc -l)

printf 'RunLine metro run   %s\n\n' "$(date '+%a %H:%M:%S')"
printf '%-14s %6s running  %6s pending\n' "SLURM tasks" \
    "$(squeue -u "$USER" -h -t R | wc -l)" "$(squeue -u "$USER" -h -t PD -r | wc -l)"
squeue -u "$USER" -h -t R -o '%P' | sort | uniq -c | awk '{printf "  on %-10s %6d\n", $2, $1}'
echo
# osmium opens every tile's output when it starts, so files existing says
# nothing; the extract job's state does.
extract_state=$(squeue -u "$USER" -h -n runline-extract -o '%T' | head -1)
tiles=$(find "$EXTRACTS" -maxdepth 1 -name '*.osm.pbf' ! -name highways.osm.pbf 2>/dev/null | wc -l)
if [ -n "$extract_state" ]; then
    printf '%-14s %s (%d tiles)\n' "Extracts" "${extract_state,,}" "$expected"
else
    printf '%-14s %6d of %d tiles\n' "Extracts" "$tiles" "$expected"
fi
printf '%-14s %6d built, %d in progress\n' "Tile graphs" \
    "$(count "$GRAPHS" '*.json')" "$(count "$GRAPHS" '*.pkl.gz.tmp')"
if [ -f "$POOLS/plan.json.gz" ]; then
    units=$(.venv/bin/python -c "import gzip,json,sys; print(len(json.load(gzip.open(sys.argv[1]))['units']))" "$POOLS/plan.json.gz")
    done_units=$(count "$POOLS" '*.jsonl.gz')
    printf '%-14s %6d of %d units (%d%%), %d in progress\n' "Pools" "$done_units" "$units" \
        "$((100 * done_units / (units > 0 ? units : 1)))" "$(count "$POOLS" '*.tmp')"
    [ -f "$POOLS/pools.sqlite" ] && printf '%-14s %s\n' "Merged" "$(du -h "$POOLS/pools.sqlite" | cut -f1)"
else
    printf '%-14s not planned yet\n' "Pools"
fi
