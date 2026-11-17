"""Fetch metro tile graphs from object storage on first use. Opt-in.

The top 50 metros' tile graphs are hundreds of GB, more than a serving VM's
disk. With RUNLINE_TILE_GRAPHS_URL set, a tile graph missing from its run's
``tile-graphs/`` is downloaded into a local disk cache the first time a
request needs it. Unset, nothing here runs and serving reads only local files.

RUNLINE_TILE_GRAPHS_URL is either a base URL laid out like RUNLINE_METROS_DIR,
fetched as ``<base>/<run>/tile-graphs/<tile>.pkl.gz``, or a template with
``{run}``, ``{tile}`` and ``{graph}`` (the tile's recorded hash) placeholders.
It must be HTTPS; plain HTTP is accepted only for a loopback host (tests).

A download goes to a temporary file in the cache directory, is checked
against the sha256 prefix the run's pools file records for that tile (the
hash merge_pools.py wrote and serving already checks), and only then renamed
into place as ``<tile>-<hash>.pkl.gz``. A file under that name has therefore
been verified; it is checked again once per process before it is used, so a
truncated or replaced file is fetched again instead of unpickled. Naming by
hash also means a rebuilt run never picks up an older run's graph.

The cache holds at most RUNLINE_TILE_DISK_CACHE_GB (default 20, in 10^9
bytes). After each download the least recently used files, by access time
(set explicitly on every use, so ``noatime`` mounts do not matter), are
removed until the cache fits, skipping any graph this process holds in memory
or is using. If those alone exceed the budget the cache stays over it.

A tile downloads once even when several requests want it: a lock per tile
within a process, and a lock file across worker processes. A failed fetch
raises MapDataUnavailable, which the API answers with a 503, and the tile is
not retried for RETRY_AFTER_SECONDS so waiting requests do not each time out.
"""

from __future__ import annotations

import hashlib
import http.client
import logging
import os
import re
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from functools import lru_cache
from pathlib import Path

from .coverage import MapDataUnavailable

LOGGER = logging.getLogger("runline.tile_fetch")

URL_ENV = "RUNLINE_TILE_GRAPHS_URL"
DEFAULT_DISK_CACHE_GB = 20.0
DEFAULT_TIMEOUT_SECONDS = 60.0
RETRY_AFTER_SECONDS = 15.0
# A temporary file this old belongs to a process that died mid-download.
STALE_PART_SECONDS = 3600.0
CHUNK_BYTES = 1 << 20
LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}
SAFE_NAME = re.compile(r"[A-Za-z0-9._-]+")
HASH = re.compile(r"[0-9a-f]{16,64}")
SUFFIX = ".pkl.gz"


def configured_url() -> str | None:
    return os.environ.get(URL_ENV, "").strip() or None


def disk_cache_dir() -> Path:
    override = os.environ.get("RUNLINE_TILE_DISK_CACHE_DIR")
    if override:
        return Path(override)
    cache = os.environ.get("RUNLINE_CACHE_DIR")
    if cache:
        return Path(cache) / "tile-graphs"
    return Path(__file__).resolve().parents[2] / "cache" / "tile-graphs"


def _positive_float(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name, default))
    except ValueError:
        return default
    return value if value > 0 else default


def disk_cache_bytes() -> int:
    return int(_positive_float("RUNLINE_TILE_DISK_CACHE_GB", DEFAULT_DISK_CACHE_GB) * 1e9)


def fetch_timeout() -> float:
    return _positive_float("RUNLINE_TILE_FETCH_TIMEOUT", DEFAULT_TIMEOUT_SECONDS)


def tile_url(base: str, run: str, tile: str, graph: str) -> str:
    """The URL of one tile graph, from a base URL or a template."""

    quoted = {"run": urllib.parse.quote(run), "tile": urllib.parse.quote(tile), "graph": graph}
    if "{" in base:
        return base.format_map(quoted)
    return f"{base.rstrip('/')}/{quoted['run']}/tile-graphs/{quoted['tile']}{SUFFIX}"


def _check_scheme(url: str) -> None:
    parts = urllib.parse.urlsplit(url)
    if parts.scheme == "https" or (parts.scheme == "http" and parts.hostname in LOOPBACK_HOSTS):
        return
    raise ValueError(f"{URL_ENV} must be an https URL, not {parts.scheme or 'no scheme'}://{parts.hostname}")


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


@lru_cache(maxsize=4096)
def _matches(path: str, modified_ns: int, size: int, expected: str) -> bool:
    """Whether a cached file still has its recorded hash; hashed once per mtime and size."""

    return _digest(Path(path)).startswith(expected)


@contextmanager
def _file_lock(path: Path) -> Iterator[None]:
    """An exclusive lock shared with other worker processes, where the OS has one."""

    try:
        import fcntl
    except ImportError:  # not POSIX: threads still download once per process
        yield
        return
    with path.open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _touch(path: Path) -> None:
    """Mark a file used now, keeping its mtime: the in-memory cache keys on it."""

    with suppress(OSError):
        os.utime(path, ns=(time.time_ns(), path.stat().st_mtime_ns))


class TileGraphFetcher:
    """Downloads tile graphs into the disk cache and keeps it within its budget."""

    def __init__(self, opener: Callable = urllib.request.urlopen) -> None:
        self.opener = opener
        self._lock = threading.Lock()
        self._tile_locks: dict[str, threading.Lock] = {}
        self._failed: dict[str, float] = {}
        self._in_use: Counter[str] = Counter()
        self.downloads = 0

    @contextmanager
    def use(
        self, run: str, tile: str, graph: str, label: str, protected: Callable[[], set[str]] = set
    ) -> Iterator[Path]:
        """The verified local copy of a tile graph, fetched if needed and kept while in use.

        ``protected`` names the files (by path) that eviction must keep, the
        graphs held in memory.
        """

        unavailable = MapDataUnavailable(f"Map data for {label} is temporarily unavailable. Try again shortly.")
        if not (SAFE_NAME.fullmatch(tile) and HASH.fullmatch(graph)):
            LOGGER.error("not fetching tile %r: unexpected tile name or graph hash %r", tile, graph)
            raise unavailable
        directory = disk_cache_dir()
        target = directory / f"{tile}-{graph}{SUFFIX}"
        if not self._pin_if_valid(target, graph):
            with self._lock:
                tile_lock = self._tile_locks.setdefault(target.name, threading.Lock())
            with tile_lock:
                if not self._pin_if_valid(target, graph):
                    self._fetch(run, tile, graph, target, unavailable)
                    if not self._pin_if_valid(target, graph):
                        raise unavailable
                    self.evict(protected())
        try:
            yield target
        finally:
            with self._lock:
                self._in_use[target.name] -= 1
                if self._in_use[target.name] <= 0:
                    del self._in_use[target.name]

    def _pin_if_valid(self, target: Path, graph: str) -> bool:
        """Pin the cached file if it is there and verified; drop it if it is not.

        Hashing happens outside the lock; the pin is taken under it, the same
        lock eviction holds, only if the file is still the one hashed.
        """

        try:
            stat = target.stat()
        except FileNotFoundError:
            return False
        if not _matches(str(target), stat.st_mtime_ns, stat.st_size, graph):
            LOGGER.warning("discarding %s: its contents do not match hash %s", target, graph)
            with suppress(FileNotFoundError):
                target.unlink()
            return False
        with self._lock:
            try:
                now = target.stat()
            except FileNotFoundError:
                return False
            if (now.st_mtime_ns, now.st_size) != (stat.st_mtime_ns, stat.st_size):
                return False
            self._in_use[target.name] += 1
            _touch(target)
            return True

    def _fetch(self, run: str, tile: str, graph: str, target: Path, unavailable: MapDataUnavailable) -> None:
        failed = self._failed.get(target.name)
        if failed is not None and time.monotonic() - failed < RETRY_AFTER_SECONDS:
            raise unavailable
        url = tile_url(configured_url() or "", run, tile, graph)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            with _file_lock(target.parent / f".{target.name}.lock"):
                # Another worker process may have fetched it while this one waited.
                if target.exists():
                    return
                started = time.monotonic()
                size = self._download(url, target, graph)
        except (OSError, ValueError, KeyError, http.client.HTTPException) as error:
            self._failed[target.name] = time.monotonic()
            LOGGER.warning("could not fetch tile graph %s from %s: %s", tile, url, error)
            raise unavailable from error
        self._failed.pop(target.name, None)
        self.downloads += 1
        LOGGER.info("fetched tile graph %s (%.1f MB) in %.1f s", tile, size / 1e6, time.monotonic() - started)

    def _download(self, url: str, target: Path, graph: str) -> int:
        """Stream ``url`` to a temporary file, verify it, and rename it to ``target``."""

        _check_scheme(url)
        timeout = fetch_timeout()
        deadline = time.monotonic() + timeout
        budget = disk_cache_bytes()
        handle, temporary = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".part")
        try:
            digest = hashlib.sha256()
            size = 0
            request = urllib.request.Request(url, headers={"User-Agent": "runline"})
            with os.fdopen(handle, "wb") as output, self.opener(request, timeout=timeout) as response:
                while chunk := response.read(CHUNK_BYTES):
                    size += len(chunk)
                    if size > budget:
                        raise ValueError("the file is larger than RUNLINE_TILE_DISK_CACHE_GB")
                    if time.monotonic() > deadline:
                        raise TimeoutError(f"download took longer than {timeout:g} s")
                    digest.update(chunk)
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            actual = digest.hexdigest()
            if not actual.startswith(graph):
                raise ValueError(f"downloaded graph hashes to {actual[:len(graph)]}, its pools were built on {graph}")
            os.replace(temporary, target)
            return size
        except BaseException:
            with suppress(FileNotFoundError):
                os.unlink(temporary)
            raise

    def evict(self, protected: set[str] = frozenset()) -> None:
        """Remove least recently used graphs until the cache fits its budget.

        Never removes a file in ``protected`` (graphs held in memory) or one a
        request in this process is using.
        """

        directory = disk_cache_dir()
        keep = {Path(path).name for path in protected if Path(path).parent == directory}
        budget = disk_cache_bytes()
        with self._lock:
            files = []
            try:
                entries = list(os.scandir(directory))
            except FileNotFoundError:
                return
            for entry in entries:
                try:
                    stat = entry.stat()
                except FileNotFoundError:
                    continue
                if entry.name.endswith(".part"):
                    if time.time() - stat.st_mtime > STALE_PART_SECONDS:
                        with suppress(FileNotFoundError):
                            os.unlink(entry.path)
                elif entry.name.endswith(SUFFIX) and not entry.name.startswith("."):
                    files.append((stat.st_atime_ns, entry.name, stat.st_size))
            total = sum(size for _, _, size in files)
            for _, name, size in sorted(files):
                if total <= budget:
                    break
                if name in keep or name in self._in_use:
                    continue
                with suppress(FileNotFoundError):
                    (directory / name).unlink()
                total -= size
                LOGGER.info("evicted tile graph %s from the disk cache", name)
            if total > budget:
                LOGGER.warning(
                    "tile graph disk cache holds %.1f GB, over its %.1f GB budget: the rest are in use",
                    total / 1e9, budget / 1e9,
                )
