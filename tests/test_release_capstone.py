"""The capstone release must refuse unverified data and uncommitted source."""

from __future__ import annotations

import hashlib
import io
import json
import subprocess
import tarfile
from pathlib import Path

import pytest

from scripts.release_capstone import (
    GRAPH_MANIFEST,
    MANIFEST,
    ReleaseError,
    canonical_json_sha256,
    commands,
    extract_release,
    load_manifest,
    require_clean_tree,
    verify_artifacts,
)


def sha(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


FILES = {
    "graphs/demo.pkl.gz": b"graph bytes",
    "pools/demo.sqlite": b"pool bytes",
    "saved-demo.html": b"<html></html>",
}
GRAPH_INDEX = {"areas": [{"slug": "demo", "file": "demo.pkl.gz"}]}


def manifest() -> dict:
    canonical = sha(json.dumps(GRAPH_INDEX, sort_keys=True, separators=(",", ":")).encode())
    return {"area": "demo", "files": {name: sha(content) for name, content in FILES.items()},
            "graph_manifest_canonical_sha256": canonical,
            "release": {"repo": "owner/repo", "tag": "t", "asset": "a.tar.gz", "sha256": "0" * 64}}


def write_artifacts(directory: Path, files: dict = FILES, index: str | None = None) -> Path:
    for name, content in files.items():
        (directory / name).parent.mkdir(parents=True, exist_ok=True)
        (directory / name).write_bytes(content)
    (directory / GRAPH_MANIFEST).write_text(index if index is not None else json.dumps(GRAPH_INDEX))
    return directory


def test_matching_artifacts_verify(tmp_path):
    verified = verify_artifacts(write_artifacts(tmp_path), manifest())
    assert set(verified) == set(FILES) | {GRAPH_MANIFEST}


def test_graph_index_formatting_does_not_matter(tmp_path):
    write_artifacts(tmp_path, index=json.dumps(GRAPH_INDEX, indent=2) + "\n")
    assert canonical_json_sha256(tmp_path / GRAPH_MANIFEST) == manifest()["graph_manifest_canonical_sha256"]
    verify_artifacts(tmp_path, manifest())


def test_a_changed_pool_is_refused(tmp_path):
    write_artifacts(tmp_path, {**FILES, "pools/demo.sqlite": b"a different pool"})
    with pytest.raises(ReleaseError, match="pools/demo.sqlite: sha256"):
        verify_artifacts(tmp_path, manifest())


def test_every_problem_is_reported_together(tmp_path):
    files = {k: v for k, v in FILES.items() if k != "saved-demo.html"}
    write_artifacts(tmp_path, {**files, "graphs/demo.pkl.gz": b"other"},
                    index=json.dumps({"areas": [{"slug": "demo", "file": "other.pkl.gz"}]}))
    with pytest.raises(ReleaseError) as error:
        verify_artifacts(tmp_path, manifest())
    message = str(error.value)
    assert "missing saved-demo.html" in message
    assert "graphs/demo.pkl.gz: sha256" in message
    assert f"{GRAPH_MANIFEST}: canonical sha256" in message


def test_staged_layout_finds_the_recorded_fallback_under_web(tmp_path):
    files = {("web/saved-demo.html" if k == "saved-demo.html" else k): v for k, v in FILES.items()}
    verify_artifacts(write_artifacts(tmp_path, files), manifest())


def test_release_extraction_copies_only_pinned_files(tmp_path):
    archive = tmp_path / "bundle.tar.gz"
    with tarfile.open(archive, "w:gz") as bundle:
        members = {**{f"runline-abc/{k}": v for k, v in FILES.items()},
                   f"runline-abc/{GRAPH_MANIFEST}": json.dumps(GRAPH_INDEX).encode(),
                   "runline-abc/start.sh": b"echo unexpected", "runline-abc/../escape.txt": b"no"}
        for name, content in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            bundle.addfile(info, io.BytesIO(content))
    extracted = extract_release(archive, tmp_path / "out", manifest())
    verify_artifacts(extracted, manifest())
    assert not (extracted / "start.sh").exists()
    assert not (tmp_path / "escape.txt").exists()


def test_release_extraction_reports_missing_files(tmp_path):
    archive = tmp_path / "bundle.tar.gz"
    with tarfile.open(archive, "w:gz"):
        pass
    with pytest.raises(ReleaseError, match="lacks"):
        extract_release(archive, tmp_path / "out", manifest())


def test_checked_in_manifest_pins_the_audited_pool():
    pinned = load_manifest(MANIFEST)
    assert pinned["files"]["pools/charlottesville-va.sqlite"] == (
        "c18a02a032cc1e4e825bea010cddc840863ec9a386f1ae4342de14801024f03c")
    assert pinned["remote_api"].startswith("https://")
    assert "vercel.app" not in pinned["remote_api"], "forwarding to a public app URL would loop"


def test_malformed_hashes_are_rejected(tmp_path):
    broken = manifest()
    broken["files"]["pools/demo.sqlite"] = "not-a-hash"
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(broken))
    with pytest.raises(ReleaseError, match="SHA-256"):
        load_manifest(path)


def git_repository(path: Path) -> Path:
    run = lambda *args: subprocess.run(["git", *args], cwd=path, check=True, capture_output=True)  # noqa: E731
    run("init", "-q")
    run("config", "user.email", "test@example.invalid")
    run("config", "user.name", "Test")
    (path / "tracked.txt").write_text("committed\n")
    run("add", "tracked.txt")
    run("commit", "-q", "-m", "initial")
    return path


def test_clean_tree_returns_head(tmp_path):
    repository = git_repository(tmp_path)
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repository, text=True).strip()
    assert require_clean_tree(repository) == head


def test_modified_tracked_file_is_refused(tmp_path):
    repository = git_repository(tmp_path)
    (repository / "tracked.txt").write_text("edited\n")
    with pytest.raises(ReleaseError, match="dirty tree"):
        require_clean_tree(repository)


def test_untracked_file_is_refused(tmp_path):
    repository = git_repository(tmp_path)
    (repository / "notes.txt").write_text("new\n")
    with pytest.raises(ReleaseError, match="notes.txt"):
        require_clean_tree(repository)


def test_staged_but_uncommitted_change_is_refused(tmp_path):
    repository = git_repository(tmp_path)
    (repository / "tracked.txt").write_text("staged\n")
    subprocess.run(["git", "add", "tracked.txt"], cwd=repository, check=True)
    with pytest.raises(ReleaseError, match="dirty tree"):
        require_clean_tree(repository)


def test_printed_deploy_never_moves_the_domain_or_promotes(tmp_path):
    steps = commands(tmp_path / "stage", "a" * 40, load_manifest(MANIFEST), "vercel")
    assert "deploy --prod --skip-domain --yes" in steps["deploy"]
    assert "promote" not in steps["deploy"]
    assert steps["promote"].startswith('vercel promote "$CANDIDATE_URL" --yes')
    assert "--expected-commit " + "a" * 40 in steps["verify_public"]
