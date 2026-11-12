"""Convert pickled walk graphs to the compact format (runline.compact).

    python scripts/compact_graphs.py graphs/charlottesville-va.pkl.gz --verify
    python scripts/compact_graphs.py ~/metros/b1/tile-graphs --verify --workers 3
    python scripts/compact_graphs.py <tile-graphs> --output <new dir> --verify
    python scripts/compact_graphs.py <tile-graphs> --check

Each ``<name>.pkl.gz`` gets a ``<name>.npz`` beside it (or in ``--output``).
The pickle is left alone: serving prefers the compact file while both exist,
and deleting the compact files rolls back. Pools need no change, because the
compact file records the pickle's hash and serving checks pools against that.

``--verify`` reloads every file it writes and compares it with the pickle:
node order, successor and predecessor order, edge keys, and every attribute
the code reads (runline.compact.compare_graphs). A file that fails is
deleted and the run exits 1. ``--check`` verifies existing compact files
without writing. A compact file already made from the same pickle is
skipped unless ``--force``.
"""

from __future__ import annotations

import argparse
import pickle
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from runline.compact import (  # noqa: E402
    PICKLE_SUFFIX,
    CompactGraphError,
    compact_path,
    compare_graphs,
    convert,
    file_fingerprint,
    read_compact,
    read_compact_header,
    read_pickle,
)

MEGABYTE = 1024 * 1024


def sources(paths: list[Path]) -> list[Path]:
    found: list[Path] = []
    for path in paths:
        if path.is_dir():
            found.extend(sorted(path.glob(f"*{PICKLE_SUFFIX}")))
        elif path.name.endswith(PICKLE_SUFFIX):
            found.append(path)
        else:
            raise SystemExit(f"{path}: not a directory or a {PICKLE_SUFFIX} file")
    return found


def destination_for(source: Path, output: Path | None) -> Path:
    target = compact_path(source)
    return output / target.name if output is not None else target


def check_one(source: Path, target: Path) -> list[str]:
    """Differences between an existing compact file and its pickle."""

    try:
        header = read_compact_header(target)
    except FileNotFoundError:
        return ["no compact file"]
    except CompactGraphError as error:
        return [str(error)]
    if header.get("source_fingerprint") != file_fingerprint(source):
        return [f"made from graph {header.get('source_fingerprint')}, not this pickle ({file_fingerprint(source)})"]
    return compare_graphs(read_pickle(source), read_compact(target))


def convert_one(job: tuple[str, str, bool, bool, bool]) -> dict:
    source, target, verify, check, force = job
    source_path, target_path = Path(source), Path(target)
    started = time.perf_counter()
    row = {"source": source, "target": target, "source_bytes": source_path.stat().st_size}
    try:
        if check:
            row.update(action="checked", problems=check_one(source_path, target_path))
        elif (
            not force
            and target_path.exists()
            and _made_from(target_path) == file_fingerprint(source_path)
            and not (verify and check_one(source_path, target_path))
        ):
            row.update(action="kept", problems=[])
        else:
            header = convert(source_path, target_path, verify=verify)
            row.update(action="written", problems=header["problems"], nodes=header["nodes"], edges=header["edges"])
    except (CompactGraphError, OSError, EOFError, ValueError, pickle.UnpicklingError) as error:
        row.update(action="failed", problems=[f"{type(error).__name__}: {error}"])
    row["compact_bytes"] = target_path.stat().st_size if target_path.exists() else 0
    row["seconds"] = round(time.perf_counter() - started, 2)
    return row


def _made_from(target: Path) -> str | None:
    try:
        return read_compact_header(target).get("source_fingerprint")
    except (CompactGraphError, OSError):
        return None


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="compact_graphs", description=__doc__.split("\n\n")[0])
    parser.add_argument("paths", type=Path, nargs="+", help=f"{PICKLE_SUFFIX} files or directories of them")
    parser.add_argument("--output", type=Path, help="write compact files here instead of beside each pickle")
    parser.add_argument("--verify", action="store_true", help="reload and compare every file written")
    parser.add_argument("--check", action="store_true", help="verify existing compact files, write nothing")
    parser.add_argument("--force", action="store_true", help="rewrite compact files that are already current")
    parser.add_argument("--workers", type=int, default=1, help="convert this many graphs at once")
    arguments = parser.parse_args(argv)
    if arguments.workers < 1:
        parser.error("--workers must be positive")

    found = sources(arguments.paths)
    if not found:
        print(f"no {PICKLE_SUFFIX} graphs found")
        return 1
    if arguments.output is not None and not arguments.check:
        arguments.output.mkdir(parents=True, exist_ok=True)
    jobs = [
        (str(source), str(destination_for(source, arguments.output)), arguments.verify, arguments.check, arguments.force)
        for source in found
    ]
    started = time.perf_counter()
    failed = 0
    totals = {"source_bytes": 0, "compact_bytes": 0}
    if arguments.workers == 1:
        rows = map(convert_one, jobs)
    else:
        pool = ProcessPoolExecutor(arguments.workers)
        rows = pool.map(convert_one, jobs)
    for row in rows:
        ok = not row["problems"]
        failed += not ok
        totals["source_bytes"] += row["source_bytes"]
        totals["compact_bytes"] += row["compact_bytes"]
        status = "ok" if ok else "FAILED"
        print(
            f"  {Path(row['source']).name}: {row['action']} {status}, "
            f"{row['source_bytes'] / MEGABYTE:.1f} MB -> {row['compact_bytes'] / MEGABYTE:.1f} MB "
            f"in {row['seconds']} s",
            flush=True,
        )
        for problem in row["problems"][:5]:
            print(f"      {problem}")
    if arguments.workers > 1:
        pool.shutdown()
    print(
        f"{len(jobs)} graphs, {failed} failed: {totals['source_bytes'] / MEGABYTE:,.1f} MB of pickles, "
        f"{totals['compact_bytes'] / MEGABYTE:,.1f} MB compact, {time.perf_counter() - started:.0f} s"
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
