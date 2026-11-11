"""Stage, verify and (optionally) deploy runline-capstone from a clean commit.

One entry point for the combined Charlottesville + DC backend that
runline-nine.vercel.app rewrites /api to. It:

1. refuses a dirty tree, then stages HEAD's committed source;
2. fetches the data from the GitHub release named in
   deploy/capstone-artifacts.json (``gh release download``), or reads a local
   copy given with --artifacts-dir;
3. verifies every file against that manifest's SHA-256 values;
4. stages the Vercel build directory with stage_presentation.stage, with
   RUNLINE_BUILD_SHA and RUNLINE_REMOTE_API in its vercel.json;
5. serves the staged directory on localhost and runs presentation_check.py and
   check_direct_routes.py against it;
6. prints the exact deploy, candidate check and promote commands.

It never deploys unless --deploy is given, and it never promotes: promotion
waits for a person to verify the candidate.

    python scripts/release_capstone.py                       # from the release
    python scripts/release_capstone.py --artifacts-dir DIR   # from a local copy
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "deploy" / "capstone-artifacts.json"
GRAPH_MANIFEST = "graphs/manifest.json"
CANDIDATE = "CANDIDATE-URL-PLACEHOLDER"
SHA256 = re.compile(r"[0-9a-f]{64}")

sys.path.insert(0, str(Path(__file__).resolve().parent))
from stage_presentation import StageError, fingerprint, stage  # noqa: E402


class ReleaseError(RuntimeError):
    """The release cannot be staged safely; nothing was deployed."""


def git(root: Path, *args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=root, text=True).strip()


def require_clean_tree(root: Path = ROOT) -> str:
    """Return HEAD's SHA, or refuse when anything is modified or untracked."""
    status = git(root, "status", "--porcelain", "--untracked-files=normal")
    if status:
        raise ReleaseError("refusing to stage from a dirty tree; commit or remove these first:\n" + status)
    return git(root, "rev-parse", "HEAD")


def is_pushed(root: Path, commit: str) -> bool:
    return bool(git(root, "branch", "--remotes", "--contains", commit))


def load_manifest(path: Path = MANIFEST) -> dict:
    manifest = json.loads(path.read_text())
    files = manifest.get("files")
    if not isinstance(files, dict) or not files:
        raise ReleaseError(f"{path} lists no files")
    pinned = {**files, "release asset": manifest["release"]["sha256"],
              GRAPH_MANIFEST: manifest["graph_manifest_canonical_sha256"]}
    for name, value in pinned.items():
        if not isinstance(value, str) or not SHA256.fullmatch(value):
            raise ReleaseError(f"{path}: {name} needs a lowercase SHA-256")
    area = manifest["area"]
    for required in (f"pools/{area}.sqlite", f"graphs/{area}.pkl.gz"):
        if required not in files:
            raise ReleaseError(f"{path} does not pin {required}")
    return manifest


def canonical_json_sha256(path: Path) -> str:
    """Hash parsed JSON, so formatting-only differences (a trailing newline) pass."""
    content = json.loads(path.read_text())
    return hashlib.sha256(json.dumps(content, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def locate(directory: Path, name: str) -> Path:
    # A staged directory keeps the recorded fallback under web/.
    path = directory / name
    if not path.exists() and name == "saved-demo.html":
        path = directory / "web" / name
    return path


def verify_artifacts(directory: Path, manifest: dict) -> dict[str, str]:
    """Check every pinned file; report all mismatches at once."""
    problems, verified = [], {}
    for name, expected in sorted(manifest["files"].items()):
        path = locate(directory, name)
        if not path.is_file():
            problems.append(f"missing {name}")
            continue
        actual = fingerprint(path)
        if actual != expected:
            problems.append(f"{name}: sha256 {actual} does not match the manifest's {expected}")
        verified[name] = actual
    graph_manifest = directory / GRAPH_MANIFEST
    if not graph_manifest.is_file():
        problems.append(f"missing {GRAPH_MANIFEST}")
    else:
        try:
            actual = canonical_json_sha256(graph_manifest)
        except ValueError as error:
            problems.append(f"{GRAPH_MANIFEST} is not JSON: {error}")
        else:
            if actual != manifest["graph_manifest_canonical_sha256"]:
                problems.append(f"{GRAPH_MANIFEST}: canonical sha256 {actual} does not match the manifest")
            verified[GRAPH_MANIFEST] = actual
    if problems:
        raise ReleaseError("artifact verification failed:\n  " + "\n  ".join(problems))
    return verified


def download_release(manifest: dict, destination: Path, repo: str | None = None, tag: str | None = None) -> Path:
    release = manifest["release"]
    repo, tag = repo or release["repo"], tag or release["tag"]
    destination.mkdir(parents=True, exist_ok=True)
    command = ["gh", "release", "download", tag, "--repo", repo, "--pattern", release["asset"],
               "--dir", str(destination)]
    print("$", shlex.join(command), flush=True)
    result = subprocess.run(command, text=True, capture_output=True)
    if result.returncode:
        raise ReleaseError(
            f"gh release download failed ({result.stderr.strip()}). Draft releases are visible only to accounts "
            f"with write access to {repo}: run `gh auth switch` or set GH_TOKEN, or use --artifacts-dir.")
    archive = destination / release["asset"]
    actual = fingerprint(archive)
    if actual != release["sha256"]:
        raise ReleaseError(f"{release['asset']}: sha256 {actual} does not match the manifest's {release['sha256']}")
    return archive


def extract_release(archive: Path, destination: Path, manifest: dict) -> Path:
    """Copy only the pinned files out of a package_presentation.py archive.

    Member names are matched against the manifest and never used as paths.
    """
    wanted = set(manifest["files"]) | {GRAPH_MANIFEST}
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive) as bundle:
        for member in bundle.getmembers():
            name = member.name.split("/", 1)[1] if "/" in member.name else ""
            if name not in wanted or not member.isfile():
                continue
            target = destination / name
            target.parent.mkdir(parents=True, exist_ok=True)
            with bundle.extractfile(member) as source, target.open("wb") as sink:
                shutil.copyfileobj(source, sink, 1 << 20)
            wanted.discard(name)
    if wanted:
        raise ReleaseError(f"{archive.name} lacks {', '.join(sorted(wanted))}")
    return destination


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def run_local_checks(stage_dir: Path, commit: str, checks: Path, *, remote_api: str | None,
                     expected_areas: list[str], python: str = sys.executable, startup: float = 180) -> dict:
    """Serve the staged directory itself and rehearse it like a deployment."""
    checks.mkdir(parents=True, exist_ok=True)
    port = free_port()
    api = f"http://127.0.0.1:{port}"
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith("RUNLINE_") and key not in ("PYTHONPATH", "VERCEL")}
    environment.update(PYTHONPATH=str(stage_dir / "src"), RUNLINE_BUILD_SHA=commit,
                       RUNLINE_ALLOW_OSM_DOWNLOAD="0", RUNLINE_CACHE_DIR=str(checks / "cache"),
                       PYTHONDONTWRITEBYTECODE="1")
    if remote_api:
        environment["RUNLINE_REMOTE_API"] = remote_api
    log = (checks / "server.log").open("w")
    server = subprocess.Popen([python, "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", str(port)],
                              cwd=stage_dir, env=environment, stdout=log, stderr=subprocess.STDOUT)
    results = {"api": api, "regional_forwarding": bool(remote_api)}
    try:
        deadline = time.monotonic() + startup
        while True:
            if server.poll() is not None:
                raise ReleaseError(f"staged app exited during startup; see {checks / 'server.log'}")
            try:
                if requests.get(api + "/api/health", timeout=2).ok:
                    break
            except requests.RequestException:
                pass
            if time.monotonic() > deadline:
                raise ReleaseError("staged app did not become healthy in time")
            time.sleep(0.5)
        loaded = requests.get(api + "/api/version", timeout=10).json()
        if loaded.get("commit") != commit:
            raise ReleaseError(f"staged app reports {loaded.get('commit')}, not {commit}")
        areas = requests.get(api + "/api/areas", timeout=30).json()["areas"]
        wanted = expected_areas if remote_api else expected_areas[:1]
        if areas != wanted:
            raise ReleaseError(f"staged app covers {areas}, expected {wanted}")
        results["areas"] = areas
        scripts = ROOT / "scripts"
        for name, command in [
            ("presentation_check", [python, str(scripts / "presentation_check.py"), "--api", api, "--require-pool",
                                    "--expected-commit", commit, "--output", str(checks / "presentation-check.json")]),
            ("check_direct_routes", [python, str(scripts / "check_direct_routes.py"), "--api", api,
                                     "--output", str(checks / "direct-check.json")]),
        ]:
            print(f"--- {name} against the staged app", flush=True)
            if subprocess.run(command, cwd=ROOT).returncode:
                raise ReleaseError(f"{name} failed against the staged app; see {checks}")
            results[name] = "passed"
    finally:
        server.terminate()
        try:
            server.wait(timeout=15)
        except subprocess.TimeoutExpired:
            server.kill()
        log.close()
    return results


def commands(stage_dir: Path, commit: str, manifest: dict, vercel: str) -> dict[str, str]:
    cli = shlex.split(vercel)
    public = manifest["vercel"]["production_url"]
    candidate = CANDIDATE
    check = ["python", "scripts/presentation_check.py", "--require-pool", "--expected-commit", commit]
    steps = {
        "deploy": shlex.join([*cli, "deploy", "--prod", "--skip-domain", "--yes", "--cwd", str(stage_dir)]),
        "verify_candidate": shlex.join([*cli, "curl", f"{candidate}/api/version", "--cwd", str(stage_dir)]),
        "promote": shlex.join([*cli, "promote", candidate, "--yes", "--cwd", str(stage_dir)]),
        "verify_public": shlex.join([*check, "--api", public, "--output", f"output/release/{commit[:12]}/public-check.json"]),
        "verify_site": shlex.join([*check, "--api", "https://runline-nine.vercel.app",
                                   "--output", f"output/release/{commit[:12]}/site-check.json"]),
    }
    return {key: value.replace(CANDIDATE, '"$CANDIDATE_URL"') for key, value in steps.items()}


def deploy(stage_dir: Path, vercel: str) -> str:
    command = [*shlex.split(vercel), "deploy", "--prod", "--skip-domain", "--yes", "--cwd", str(stage_dir)]
    print("$", shlex.join(command), flush=True)
    result = subprocess.run(command, text=True, capture_output=True)
    sys.stderr.write(result.stderr)
    if result.returncode:
        raise ReleaseError("vercel deploy failed; nothing was promoted")
    urls = re.findall(r"https://\S+\.vercel\.app", result.stdout)
    if not urls:
        raise ReleaseError(f"vercel deploy printed no deployment URL:\n{result.stdout}")
    return urls[-1]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--artifacts-dir", type=Path, help="local copy (graphs/, pools/, saved-demo.html) instead of the release")
    source.add_argument("--release-tag", help="override the manifest's release tag")
    parser.add_argument("--repo", help="override the manifest's GitHub repository")
    parser.add_argument("--manifest", type=Path, default=MANIFEST)
    parser.add_argument("--output", type=Path, help="fresh work directory (default output/release/<sha>)")
    parser.add_argument("--project-config", type=Path, help="Vercel project.json (default: written from the manifest)")
    parser.add_argument("--check-remote", action="store_true",
                        help="forward coverage misses to the manifest's regional API during local checks")
    parser.add_argument("--skip-local-checks", action="store_true", help="stage only; --deploy refuses this")
    parser.add_argument("--vercel", default="vercel", help="Vercel CLI command, e.g. 'npx vercel@latest'")
    parser.add_argument("--deploy", action="store_true",
                        help="also run the deploy command (never promotes); requires a pushed commit and passing checks")
    args = parser.parse_args(argv)
    if args.deploy and args.skip_local_checks:
        parser.error("--deploy requires the local checks")

    try:
        manifest = load_manifest(args.manifest)
        commit = require_clean_tree(ROOT)
        pushed = is_pushed(ROOT, commit)
        if args.deploy and not pushed:
            raise ReleaseError(f"{commit[:12]} is on no remote branch; push it so the deployed SHA is reproducible")
        work = (args.output or ROOT / "output" / "release" / commit[:12]).resolve()
        if work.exists():
            raise ReleaseError(f"{work} already exists; choose a fresh --output")
        work.mkdir(parents=True)
        print(f"Source commit {commit}{'' if pushed else ' (not on any remote branch yet)'}", flush=True)
        if args.artifacts_dir:
            artifacts, origin = args.artifacts_dir.resolve(), f"local directory {args.artifacts_dir}"
        else:
            tag = args.release_tag or manifest["release"]["tag"]
            archive = download_release(manifest, work / "download", args.repo, tag)
            print(f"Verified {archive.name} sha256 {manifest['release']['sha256']}", flush=True)
            artifacts, origin = extract_release(archive, work / "artifacts", manifest), f"release {tag}"
        verified = verify_artifacts(artifacts, manifest)
        for name, value in verified.items():
            print(f"  ok {value[:16]}  {name}", flush=True)
        project_config = args.project_config
        if project_config is None:
            project_config = work / "project.json"
            vercel_ids = {key: manifest["vercel"][key] for key in ("orgId", "projectId")}
            project_config.write_text(json.dumps({**vercel_ids, "projectName": manifest["vercel"]["project"]}) + "\n")
        area = manifest["area"]
        stage_dir = work / "stage"
        report = stage(artifacts / "graphs", artifacts / "pools" / f"{area}.sqlite", stage_dir, project_config,
                       offline_demo=locate(artifacts, "saved-demo.html"), remote_api=manifest["remote_api"],
                       verified_pools_sha256=manifest["files"][f"pools/{area}.sqlite"])
        report["artifacts"] = {"source": origin, "manifest": str(args.manifest.resolve().relative_to(ROOT))
                               if args.manifest.resolve().is_relative_to(ROOT) else str(args.manifest),
                               "verified": verified}
        if args.skip_local_checks:
            report["local_checks"] = "skipped"
        else:
            report["local_checks"] = run_local_checks(
                stage_dir, commit, work / "checks", remote_api=manifest["remote_api"] if args.check_remote else None,
                expected_areas=manifest["expected_areas"])
        (stage_dir / "release.json").write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(report, indent=2), flush=True)
    except (ReleaseError, StageError, requests.RequestException) as error:
        print(f"REFUSED: {error}", file=sys.stderr)
        return 1

    steps = commands(stage_dir, commit, manifest, args.vercel)
    if args.deploy:
        try:
            candidate = deploy(stage_dir, args.vercel)
        except ReleaseError as error:
            print(f"REFUSED: {error}", file=sys.stderr)
            return 1
        steps.pop("deploy")
        print(f"\nDeployed candidate {candidate} (not promoted).\n  export CANDIDATE_URL={shlex.quote(candidate)}")
    print("\nNext steps (run from this checkout; nothing below has been run):")
    for key, label in [("deploy", "1. Deploy a production candidate without moving the domain"),
                       ("verify_candidate", "2. export CANDIDATE_URL=<URL printed by step 1>, then confirm the "
                                            "protected candidate reports the staged commit"),
                       ("promote", "3. Promote it to runline-capstone.vercel.app"),
                       ("verify_public", "4. Rehearse the public capstone URL"),
                       ("verify_site", "5. Rehearse the canonical site through its /api rewrite")]:
        if key in steps:
            print(f"\n{label}:\n  {steps[key]}")
    print("\nRollback: see docs/unified-deployment.md (vercel rollback / promote the previous deployment).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
