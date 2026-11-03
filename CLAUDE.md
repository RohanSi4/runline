# RunLine

Running-loop generator over OpenStreetMap (CS 4971 capstone: Adithya
Balasubramaniam, Krish Patel, Rohan Singh). Python/FastAPI on Vercel; see
README.md for setup and architecture.

Route pools are precomputed on the UVA CS SLURM cluster; `slurm/README.md`
is the runbook and lists the cluster's gotchas.

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
