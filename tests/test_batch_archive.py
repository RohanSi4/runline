"""Archiving a finished batch: manifest, verify, pack, stage, smoke.

A tiny batch is built the way tests/test_metros.py builds its run (two toy
metros either side of the Charlottesville map centre, tile graphs cropped
from the shipped graph), but laid out as slurm/run_batch.sh leaves one:
<base>/batches/toy/pools/ and the shared <base>/tile-graphs/.
"""

from __future__ import annotations

import gzip
import json
import math
import os
import pickle
import re
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest
from shapely import affinity
from shapely.geometry import Point, mapping

from runline import coverage, serving
from runline.coverage import areas, load_area_graph
from runline.geo import destination
from runline.models import Coordinate
from runline.osm import crop_to_radius
from runline.pools import decode_pool
from runline.tiling import plan_tiles
from scripts import batch_manifest, merge_pools, plan_tile_pools, precompute_pools, stage_metro_run

REPO = Path(__file__).resolve().parents[1]
MAX_MILES = 3.0
NAMES = {"west-toy": "West Toy, VA", "east-toy": "East Toy, VA"}


def _circle(centre: Coordinate, radius_km: float):
    circle = Point(centre.longitude, centre.latitude).buffer(1.0, quad_segs=32)
    return affinity.scale(
        circle, radius_km / (111.32 * math.cos(math.radians(centre.latitude))), radius_km / 110.54
    )


@pytest.fixture(scope="module")
def batch(tmp_path_factory) -> dict:
    root = tmp_path_factory.mktemp("archive")
    area = next(item for item in areas() if item.slug == "charlottesville-va")
    graph = load_area_graph(area)
    shapes = {
        "west-toy": _circle(destination(area.center, 270, 1500), 0.5),
        "east-toy": _circle(destination(area.center, 90, 1500), 0.5),
    }
    config = {"grid_meters": 200.0, "max_miles": MAX_MILES, "metros": []}
    for slug, shape in shapes.items():
        plan = plan_tiles(shape, slug, max_miles=MAX_MILES, core_meters=1_000.0)
        counts = np.bincount(plan.owner, minlength=len(plan.tiles))
        config["metros"].append({
            "slug": slug,
            "name": NAMES[slug],
            "tiles": [tile.to_json(slug, NAMES[slug], int(count)) for tile, count in zip(plan.tiles, counts)],
        })
    (root / "metros.json").write_text(json.dumps(config))
    (root / "boundaries.geojson").write_text(json.dumps({
        "type": "FeatureCollection",
        "features": [
            {"type": "Feature", "properties": {"slug": slug}, "geometry": mapping(shape)}
            for slug, shape in shapes.items()
        ],
    }))

    base = root / "base"
    pools = base / "batches" / "toy" / "pools"
    assert plan_tile_pools.main([
        "--metros", str(root / "metros.json"), "--boundaries", str(root / "boundaries.geojson"),
        "--batch", *shapes, "--output", str(pools),
    ]) == 0
    with gzip.open(pools / "plan.json.gz", "rt") as stream:
        plan = json.load(stream)
    graphs = base / "tile-graphs"
    graphs.mkdir()
    for slug, tile in plan["tiles"].items():
        cropped = crop_to_radius(graph, Coordinate(tile["latitude"], tile["longitude"]), tile["radius_meters"])
        (graphs / f"{slug}.pkl.gz").write_bytes(gzip.compress(pickle.dumps(cropped, protocol=5), 1, mtime=0))
        (graphs / f"{slug}.json").write_text(json.dumps({"tile": slug, "nodes": cropped.number_of_nodes()}))
    # Shards land beside the plan, as on the cluster.
    assert precompute_pools.main([
        "--plan", str(pools / "plan.json.gz"), "--tile-graphs", str(graphs), "--output", str(pools),
        "--targets", "2", "3", "--surfaces", "mixed", "--limit-cells", "2",
    ]) == 0
    assert merge_pools.main(["--plan", str(pools / "plan.json.gz"), "--input", str(pools)]) == 0
    assert batch_manifest.main([str(base / "batches" / "toy")]) == 0
    manifest = json.loads((base / "batches" / "toy" / "manifest.json").read_text())
    return {"root": root, "base": base, "plan": plan, "manifest": manifest}


def _copy(batch, tmp_path: Path) -> Path:
    """A copy of the archived files, as a transfer would leave them; returns its batch directory."""

    copy = tmp_path / "copy"
    shutil.copytree(batch["base"], copy)
    return copy / "batches" / "toy"


def _pooled_tile(batch) -> str:
    return next(tile for tile, entry in batch["manifest"]["tile_graphs"].items() if entry["pooled"])


def test_the_manifest_lists_every_file_and_the_batch_facts(batch) -> None:
    manifest, plan, base = batch["manifest"], batch["plan"], batch["base"]
    paths = {entry["path"] for entry in manifest["files"]}

    assert {"batches/toy/pools/plan.json.gz", "batches/toy/pools/pools.sqlite"} <= paths
    assert {f"batches/toy/pools/{unit['id']}.jsonl.gz" for unit in plan["units"]} <= paths
    assert {f"tile-graphs/{tile}.pkl.gz" for tile in plan["tiles"]} <= paths
    assert manifest["plan"]["cells"] == sum(len(unit["cells"]) for unit in plan["units"])
    assert manifest["plan"]["tiles"] == len(plan["tiles"])
    assert manifest["shards"] == len(plan["units"])
    pools = manifest["pools"]
    assert pools["quick_check"] == "ok" and pools["cells_index"] is True
    assert pools["indexed_cells"] == manifest["plan"]["cells"]
    assert 0 < pools["pool_cells"] <= pools["pools"] and pools["loops"] > 0
    assert manifest["problems"] == []
    # The tile hashes are the ones serving computes and the merge recorded.
    database = sqlite3.connect(base / "batches/toy/pools/pools.sqlite")
    recorded = dict(database.execute("SELECT tile, graph FROM tiles"))
    database.close()
    for tile, entry in manifest["tile_graphs"].items():
        assert entry["file"] == entry["graph"] == recorded[tile]
        assert entry["file"] == serving._fingerprint(base / "tile-graphs" / f"{tile}.pkl.gz")


def test_a_copy_verifies_against_its_manifest(batch, tmp_path, capsys) -> None:
    copy = _copy(batch, tmp_path)

    assert batch_manifest.main(["--verify", str(copy)]) == 0
    assert "verified" in capsys.readouterr().out


def test_a_corrupted_file_fails_verification(batch, tmp_path, capsys) -> None:
    copy = _copy(batch, tmp_path)
    shard = sorted((copy / "pools").glob("*.jsonl.gz"))[0]
    data = bytearray(shard.read_bytes())
    data[len(data) // 2] ^= 0xFF
    shard.write_bytes(bytes(data))

    assert batch_manifest.main(["--verify", str(copy)]) == 1
    out = capsys.readouterr().out
    assert f"batches/toy/pools/{shard.name} sha256" in out and "verify FAILED" in out


def test_a_truncated_pool_file_fails_verification(batch, tmp_path, capsys) -> None:
    copy = _copy(batch, tmp_path)
    pools = copy / "pools" / "pools.sqlite"
    pools.write_bytes(pools.read_bytes()[: pools.stat().st_size // 2])

    assert batch_manifest.main(["--verify", str(copy)]) == 1
    assert "pools.sqlite is" in capsys.readouterr().out


def test_a_missing_tile_graph_fails_verification(batch, tmp_path, capsys) -> None:
    copy = _copy(batch, tmp_path)
    tile = _pooled_tile(batch)
    (copy.parent.parent / "tile-graphs" / f"{tile}.pkl.gz").unlink()

    assert batch_manifest.main(["--verify", str(copy)]) == 1
    out = capsys.readouterr().out
    assert f"missing tile-graphs/{tile}.pkl.gz" in out


def test_a_tile_graph_that_is_not_its_pools_graph_fails(batch, tmp_path, capsys) -> None:
    copy = _copy(batch, tmp_path)
    tile = _pooled_tile(batch)
    other = next(slug for slug in batch["plan"]["tiles"] if slug != tile)
    graphs = copy.parent.parent / "tile-graphs"
    shutil.copy(graphs / f"{other}.pkl.gz", graphs / f"{tile}.pkl.gz")

    assert batch_manifest.main(["--verify", str(copy)]) == 1
    out = capsys.readouterr().out
    assert f"tile graph {tile} has fingerprint" in out

    # The same batch on scratch (a tile rebuilt after its pools) gets no manifest.
    (copy / "manifest.json").unlink()
    assert batch_manifest.main([str(copy)]) == 1
    out = capsys.readouterr().out
    assert f"tile graph {tile} has fingerprint" in out and "no manifest written" in out
    assert not (copy / "manifest.json").exists()


def test_a_batch_with_a_missing_shard_gets_no_manifest_unless_allowed(batch, tmp_path, capsys) -> None:
    copy = _copy(batch, tmp_path)
    (copy / "manifest.json").unlink()
    shard = sorted((copy / "pools").glob("*.jsonl.gz"))[0]
    shard.unlink()

    assert batch_manifest.main([str(copy)]) == 1
    assert not (copy / "manifest.json").exists()
    assert batch_manifest.main([str(copy), "--allow-problems"]) == 1
    manifest = json.loads((copy / "manifest.json").read_text())
    assert manifest["problems"] == [f"missing batches/toy/pools/{shard.name}"]
    capsys.readouterr()


def _archive(env: dict, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(REPO / "slurm" / "archive_batch.sh"), *args],
        env={**os.environ, "CLUSTER": "cs", "PY": sys.executable, **env},
        capture_output=True, text=True,
    )


def test_the_archive_script_parses() -> None:
    assert subprocess.run(["bash", "-n", str(REPO / "slurm" / "archive_batch.sh")]).returncode == 0


def test_a_dry_run_prints_the_jobs_and_the_pull_and_changes_nothing(tmp_path) -> None:
    base = tmp_path / "base"
    base.mkdir()

    result = _archive({"DRY_RUN": "1", "BASE": str(base)}, "b1", "/drive/runline")

    assert result.returncode == 0, result.stderr
    out = result.stdout + result.stderr
    submits = [line for line in out.splitlines() if line.startswith("[dry run] sbatch")]
    assert len(submits) == 2
    assert "--partition=cpu,gpu" in submits[0] and "--job-name=runline-manifest" in submits[0]
    assert "archive_batch.sh --manifest b1" in submits[0]
    assert "--dependency=afterok:DRY1-runline-manifest" in submits[1] and "--pack b1" in submits[1]
    assert re.search(r"rsync -a --partial .*ControlMaster=auto.*ControlPath=~/\.ssh/cm-%r@%h:%p", out)
    assert f"{base}/archives/b1/ /drive/runline/b1/" in out
    assert "batch_manifest.py --verify /drive/runline/b1/unpacked/batches/b1" in out
    assert list(base.iterdir()) == []


def test_a_dry_run_here_prints_the_manifest_and_pack_commands(tmp_path) -> None:
    result = _archive({"DRY_RUN": "1", "HERE": "1", "BASE": str(tmp_path)}, "b1")

    assert result.returncode == 0, result.stderr
    assert f"scripts/batch_manifest.py {tmp_path}/batches/b1 --jobs 4" in result.stderr
    assert "| tar -C" in result.stderr and "split -a 3 -b 3G" in result.stderr
    assert "sbatch" not in result.stderr
    assert list(tmp_path.iterdir()) == []


def test_packed_parts_unpack_to_a_copy_that_verifies(batch, tmp_path) -> None:
    base = tmp_path / "scratch"
    shutil.copytree(batch["base"], base)
    (base / "batches" / "toy" / "manifest.json").unlink()

    result = _archive({"HERE": "1", "BASE": str(base), "PART_SIZE": "64k"}, "toy")

    assert result.returncode == 0, result.stdout + result.stderr
    archive = base / "archives" / "toy"
    parts = sorted(archive.glob("toy.tar.*"))
    assert len(parts) > 1 and (archive / "toy-manifest.json").exists()
    sums = (archive / "toy.parts.sha256").read_text().split()
    assert sums[1::2] == [part.name for part in parts]
    unpacked = tmp_path / "unpacked"
    unpacked.mkdir()
    subprocess.run(f"cat {archive}/toy.tar.* | tar -xf - -C {unpacked}", shell=True, check=True)
    assert batch_manifest.main(["--verify", str(unpacked / "batches" / "toy")]) == 0
    # Only the batch's files travel: no extracts, no archive parts.
    assert sorted(path.name for path in unpacked.iterdir()) == ["batches", "tile-graphs"]


def test_staging_splits_the_batch_per_metro_and_serves_from_pools(batch, tmp_path, monkeypatch, capsys) -> None:
    copy = _copy(batch, tmp_path)
    metros_dir = tmp_path / "metros"
    monkeypatch.delenv("RUNLINE_REMOTE_API", raising=False)
    try:
        assert stage_metro_run.main([str(copy), "--metros-dir", str(metros_dir), "--smoke", "2"]) == 0
    finally:
        coverage._read_manifest.cache_clear()
    out = capsys.readouterr().out

    assert sorted(path.name for path in metros_dir.iterdir()) == ["east-toy", "west-toy"]
    smoked = [line for line in out.splitlines() if " smoke " in line]
    assert len(smoked) == 4 and all("source pool" in line for line in smoked)
    total = 0
    for metro in ("east-toy", "west-toy"):
        database = sqlite3.connect(metros_dir / metro / "pools.sqlite")
        tiles = {row[0] for row in database.execute("SELECT DISTINCT metro FROM tiles")}
        labels = json.loads(database.execute("SELECT value FROM meta WHERE key = 'metros'").fetchone()[0])
        cell_tiles = {row[0] for row in database.execute("SELECT DISTINCT tile FROM cells")}
        total += database.execute("SELECT COUNT(*) FROM pools").fetchone()[0]
        database.close()
        assert tiles == {metro} and labels == {metro: NAMES[metro]}
        assert cell_tiles <= {path.name.removesuffix(".pkl.gz") for path in (metros_dir / metro / "tile-graphs").glob("*.pkl.gz")}
    assert total == batch["manifest"]["pools"]["pools"]
    # Nothing printed locates a start: no coordinates, no cell ids.
    for blob in _pool_blobs(batch):
        latitude, longitude = decode_pool(blob)["origin"]
        assert f"{latitude:.3f}" not in out and f"{longitude:.3f}" not in out
    database = sqlite3.connect(batch["base"] / "batches/toy/pools/pools.sqlite")
    cells = [row[0] for row in database.execute("SELECT cell FROM pools")]
    database.close()
    assert not any(cell in out for cell in cells)


def _pool_blobs(batch) -> list[bytes]:
    database = sqlite3.connect(batch["base"] / "batches/toy/pools/pools.sqlite")
    try:
        return [row[0] for row in database.execute("SELECT pool FROM pools")]
    finally:
        database.close()


def test_staging_refuses_an_archive_that_fails_verification(batch, tmp_path, capsys) -> None:
    copy = _copy(batch, tmp_path)
    (copy.parent.parent / "tile-graphs" / f"{_pooled_tile(batch)}.pkl.gz").unlink()

    assert stage_metro_run.main([str(copy), "--metros-dir", str(tmp_path / "metros")]) == 1
    assert "not staging" in capsys.readouterr().out
    assert not (tmp_path / "metros").exists()


def test_staging_over_an_existing_run_needs_replace(batch, tmp_path, capsys) -> None:
    copy = _copy(batch, tmp_path)
    metros_dir = tmp_path / "metros"
    (metros_dir / "east-toy").mkdir(parents=True)

    assert stage_metro_run.main([str(copy), "--metros-dir", str(metros_dir), "--skip-verify", "--smoke", "0"]) == 1
    assert stage_metro_run.main([
        str(copy), "--metros-dir", str(metros_dir), "--skip-verify", "--smoke", "0", "--replace",
        "--metro", "east-toy", "--link", "symlink",
    ]) == 0
    assert any(path.name.startswith("east-toy.replaced-") for path in metros_dir.iterdir())
    assert all(path.is_symlink() for path in (metros_dir / "east-toy" / "tile-graphs").iterdir())
    capsys.readouterr()
