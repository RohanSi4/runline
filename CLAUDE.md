# RunLine

Running-loop generator over OpenStreetMap (CS 4971 capstone: Adithya
Balasubramaniam, Krish Patel, Rohan Singh). Python/FastAPI on Vercel; see
README.md for setup and architecture.

## Current effort: US route-pool precompute

Read `docs/us-precompute-handoff.md` before working on pools, graph builds,
the SLURM scripts, or routing performance. It has the status, measured
baselines, cluster gotchas, and the remaining work split into tasks with
acceptance criteria. When you finish a task or measure something, update its
Status and Measured sections in the same PR.

## Rules

- Never push to `main`. One branch and PR per task.
- Before merging anything that touches `src/runline/osm.py`, `scoring.py` or
  `models.py`, run the tests, `scripts/bench.py` and `scripts/shape_audit.py`
  and put before/after numbers in the PR.
- No real coordinates or addresses in tracked files; private benchmark starts
  live in `.private/benchmarks.local.json` (a test enforces this).
- `requirements.txt` pins exact versions because pickled graphs only load
  under the versions that built them. Rebuild graphs if you bump networkx or
  shapely.
- Do not use the cluster's `python` module; `slurm/setup.sh` explains why.
