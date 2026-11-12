"""Compact walk graphs: flat numpy arrays in place of a pickled networkx graph.

A graph file is either the pickle the builders write (``<name>.pkl.gz``) or
its compact twin beside it (``<name>.npz``), made by scripts/compact_graphs.py.
The compact file keeps only the attributes the planner, serving and the
audits read (``NODE_TAGS``, ``EDGE_TAGS``, coordinates, elevation, length and
geometry) and rebuilds a ``networkx.MultiDiGraph`` with the same nodes,
edges, keys and adjacency order as the pickle, so every search, tie-break and
measurement comes out the same. docs/compact-tiles.md is the format spec.

Pools record the 16-hex sha256 prefix of the graph file they were built on.
A compact file carries that hash of the pickle it was made from
(``source_fingerprint``) plus a sha256 of its own content, so pools built on
the pickle keep validating after conversion, and a damaged compact file is
refused instead of served:

- ``graph_file`` picks the file to serve for a configured ``.pkl.gz`` name:
  the compact twin when it exists and is current, else the pickle;
- ``graph_fingerprint`` is the hash pools are checked against: the pickle's
  bytes, or a verified compact file's recorded source hash;
- ``read_graph`` loads either kind, verifying a compact file's content hash.

Unlike a pickle, loading a compact file runs no code: arrays are read with
``allow_pickle=False`` and strings travel as JSON.
"""

from __future__ import annotations

import functools
import gzip
import hashlib
import io
import json
import logging
import math
import os
import pickle
import zipfile
import zlib
from pathlib import Path
from typing import Any

import networkx as nx
import numpy as np

LOGGER = logging.getLogger("runline.compact")

FORMAT = "runline-compact-graph"
VERSION = 1
PICKLE_SUFFIX = ".pkl.gz"
COMPACT_SUFFIX = ".npz"

# Every node and edge attribute the code reads (docs/compact-tiles.md lists
# where). Anything else in the pickle is dropped by conversion.
NODE_TAGS = ("highway", "crossing", "crossing:signals")
EDGE_TAGS = ("highway", "name", "ref", "surface", "bridge", "tunnel")
NODE_FLOATS = ("elevation",)
EDGE_FLOATS = ("length",)
# Coordinates of nodes and of edge geometry; geometry is its own section.
COORDINATES = ("x", "y")
# Degrees in OSM have seven decimals. Stored as integers of 1e-7 degrees
# when every value comes back bit for bit, else as float64.
COORDINATE_SCALE = 10_000_000


class CompactGraphError(ValueError):
    """A compact graph file that is unreadable, damaged or not this format."""


def is_compact(path: Path | str) -> bool:
    return str(path).endswith(COMPACT_SUFFIX)


def compact_path(path: Path | str) -> Path:
    """``<name>.npz`` for ``<name>.pkl.gz`` (or the path itself if compact)."""

    path = Path(path)
    if is_compact(path):
        return path
    name = path.name.removesuffix(PICKLE_SUFFIX)
    return path.with_name(name + COMPACT_SUFFIX)


def pickle_path(path: Path | str) -> Path:
    path = Path(path)
    if not is_compact(path):
        return path
    return path.with_name(path.name.removesuffix(COMPACT_SUFFIX) + PICKLE_SUFFIX)


def file_fingerprint(path: Path | str) -> str:
    """sha256 of a file's bytes, first 16 hex digits: what pools record."""

    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()[:16]


# --- writing -----------------------------------------------------------------


def _codes_dtype(size: int):
    for dtype in (np.uint8, np.uint16, np.uint32):
        if size <= np.iinfo(dtype).max + 1:
            return dtype
    return np.uint64


def _json_value(value: Any) -> Any:
    """``value`` if JSON gives it back exactly (type included), else raise."""

    if isinstance(value, bool) or value is None or isinstance(value, (str, int)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    if isinstance(value, list) and all(
        isinstance(item, (str, int, bool)) or item is None for item in value
    ):
        return value
    raise CompactGraphError(f"cannot store attribute value {value!r} ({type(value).__name__})")


def _tag_section(values: list[tuple[bool, Any]]) -> tuple[list, np.ndarray]:
    """A vocabulary (index 0 means "attribute absent") and one code per item."""

    vocabulary: list = [None]
    lookup: dict = {}
    codes = np.zeros(len(values), dtype=np.uint64)
    for position, (present, value) in enumerate(values):
        if not present:
            continue
        value = _json_value(value)
        key = (type(value).__name__, tuple(value) if isinstance(value, list) else value)
        code = lookup.get(key)
        if code is None:
            code = lookup[key] = len(vocabulary)
            vocabulary.append(value)
        codes[position] = code
    return vocabulary, codes.astype(_codes_dtype(len(vocabulary)))


def _float_section(values: list[tuple[bool, Any]]) -> tuple[np.ndarray, np.ndarray]:
    present = np.fromiter((flag for flag, _ in values), dtype=bool, count=len(values))
    numbers = []
    for flag, value in values:
        if flag and (isinstance(value, bool) or not isinstance(value, (int, float, np.floating))):
            raise CompactGraphError(f"cannot store {value!r} as a number")
        numbers.append(float(value) if flag else 0.0)
    return np.asarray(numbers, dtype=np.float64), present.astype(np.uint8)


def _coordinate_arrays(name: str, values: np.ndarray, arrays: dict, encodings: dict) -> None:
    """Exact fixed point, delta coded, when it round-trips; float64 otherwise."""

    values = np.ascontiguousarray(values, dtype=np.float64)
    if values.size and np.all(np.isfinite(values)) and np.max(np.abs(values)) < 180.5:
        fixed = np.rint(values * COORDINATE_SCALE).astype(np.int64)
        deltas = np.diff(fixed, prepend=0)
        if np.array_equal(fixed / COORDINATE_SCALE, values) and np.max(np.abs(deltas)) < 2**31:
            arrays[name] = deltas.astype(np.int32)
            encodings[name] = "delta-fixed"
            return
    arrays[name] = values
    encodings[name] = "float64"


def _decode_coordinates(name: str, arrays: dict, encodings: dict) -> np.ndarray:
    encoding = encodings[name]
    if encoding == "delta-fixed":
        return np.cumsum(arrays[name], dtype=np.int64) / COORDINATE_SCALE
    if encoding == "float64":
        return arrays[name].astype(np.float64, copy=False)
    raise CompactGraphError(f"unknown coordinate encoding {encoding!r}")


def _content_digest(header: dict, arrays: dict[str, np.ndarray]) -> str:
    """sha256 over the header (less this digest) and every array's bytes."""

    digest = hashlib.sha256(FORMAT.encode() + b"\0")
    body = {key: value for key, value in header.items() if key != "content_sha256"}
    digest.update(json.dumps(body, sort_keys=True, separators=(",", ":")).encode())
    for name in sorted(arrays):
        array = np.ascontiguousarray(arrays[name])
        digest.update(f"\0{name}\0{array.dtype.str}\0{array.shape}\0".encode())
        digest.update(array.tobytes())
    return digest.hexdigest()


def graph_arrays(graph: nx.MultiDiGraph) -> tuple[dict, dict[str, np.ndarray]]:
    """The compact header (without hashes) and arrays for a walk graph."""

    if not isinstance(graph, nx.MultiDiGraph):
        raise CompactGraphError(f"only MultiDiGraph is supported, not {type(graph).__name__}")
    nodes = list(graph._node)
    index = {node: position for position, node in enumerate(nodes)}
    if not all(isinstance(node, int) and not isinstance(node, bool) for node in nodes):
        raise CompactGraphError("node ids must be integers")
    node_data = [graph._node[node] for node in nodes]

    arrays: dict[str, np.ndarray] = {"node_ids": np.asarray(nodes, dtype=np.int64)}
    encodings: dict[str, str] = {}
    vocabularies: dict[str, list] = {}
    for axis in COORDINATES:
        _coordinate_arrays(
            f"node_{axis}", np.fromiter((float(data[axis]) for data in node_data), np.float64, len(nodes)),
            arrays, encodings,
        )
    for name in NODE_FLOATS:
        values, present = _float_section([(name in data, data.get(name)) for data in node_data])
        arrays[f"node_{name}"], arrays[f"node_{name}_present"] = values, present
    for name in NODE_TAGS:
        vocabularies[f"node.{name}"], arrays[f"node_tag_{name}"] = _tag_section(
            [(name in data, data.get(name)) for data in node_data]
        )

    # Edges in the graph's own order: node by node, each successor in
    # insertion order, each key in insertion order.
    offsets = [0]
    targets: list[int] = []
    keys: list[Any] = []
    edge_data: list[dict] = []
    for node in nodes:
        for successor, keydict in graph._succ[node].items():
            for key, data in keydict.items():
                targets.append(index[successor])
                keys.append(key)
                edge_data.append(data)
        offsets.append(len(targets))
    if not all(isinstance(key, int) and not isinstance(key, bool) for key in keys):
        raise CompactGraphError("edge keys must be integers")
    arrays["succ_offsets"] = np.asarray(offsets, dtype=np.int64)
    arrays["edge_target"] = np.asarray(targets, dtype=np.int32 if len(nodes) < 2**31 else np.int64)
    arrays["edge_key"] = np.asarray(keys, dtype=np.int64).astype(
        np.uint8 if not keys or (min(keys) >= 0 and max(keys) < 256) else np.int64
    )
    # Predecessor order is its own: serving searches the reversed graph, and
    # a search's ties follow neighbour order.
    pred_offsets = [0]
    pred_sources: list[int] = []
    for node in nodes:
        pred_sources.extend(index[predecessor] for predecessor in graph._pred[node])
        pred_offsets.append(len(pred_sources))
    arrays["pred_offsets"] = np.asarray(pred_offsets, dtype=np.int64)
    arrays["pred_sources"] = np.asarray(pred_sources, dtype=arrays["edge_target"].dtype)

    for name in EDGE_FLOATS:
        values, present = _float_section([(name in data, data.get(name)) for data in edge_data])
        arrays[f"edge_{name}"], arrays[f"edge_{name}_present"] = values, present
    for name in EDGE_TAGS:
        vocabularies[f"edge.{name}"], arrays[f"edge_tag_{name}"] = _tag_section(
            [(name in data, data.get(name)) for data in edge_data]
        )

    # A two-way street's edges carry the same line, one reversed, so each
    # distinct line is stored once and every edge with a geometry names its
    # line and direction.
    has_geometry = np.zeros(len(edge_data), dtype=np.uint8)
    line_of_edge: list[int] = []
    reversed_line: list[int] = []
    line_offsets = [0]
    lines: dict[bytes, int] = {}
    xs: list[np.ndarray] = []
    ys: list[np.ndarray] = []
    for position, data in enumerate(edge_data):
        if "geometry" not in data:
            continue
        geometry = data["geometry"]
        if getattr(geometry, "geom_type", None) != "LineString" or geometry.has_z:
            raise CompactGraphError(f"edge geometry must be a 2D LineString, not {geometry!r}")
        coordinates = np.ascontiguousarray(np.asarray(geometry.coords, dtype=np.float64).reshape(-1, 2))
        if len(coordinates) < 2:
            raise CompactGraphError(f"edge geometry needs two points, not {geometry!r}")
        has_geometry[position] = 1
        forward = coordinates.tobytes()
        line = lines.get(forward)
        backward = False
        if line is None:
            line = lines.get(np.ascontiguousarray(coordinates[::-1]).tobytes())
            backward = line is not None
        if line is None:
            line = lines[forward] = len(line_offsets) - 1
            xs.append(coordinates[:, 0])
            ys.append(coordinates[:, 1])
            line_offsets.append(line_offsets[-1] + len(coordinates))
        line_of_edge.append(line)
        reversed_line.append(int(backward))
    arrays["edge_has_geometry"] = has_geometry
    arrays["geometry_line"] = np.asarray(line_of_edge, dtype=np.int32 if len(line_offsets) < 2**31 else np.int64)
    arrays["geometry_reversed"] = np.asarray(reversed_line, dtype=np.uint8)
    arrays["line_offsets"] = np.asarray(line_offsets, dtype=np.int64)
    for axis, parts in (("x", xs), ("y", ys)):
        _coordinate_arrays(
            f"geometry_{axis}", np.concatenate(parts) if parts else np.zeros(0), arrays, encodings
        )

    attributes = {}
    for key, value in graph.graph.items():
        if str(key).startswith("_runline"):
            continue  # per-process memos, never part of a built graph
        if not isinstance(key, str):
            raise CompactGraphError(f"graph attribute name {key!r} is not a string")
        attributes[key] = _json_value(value)
    header = {
        "format": FORMAT,
        "version": VERSION,
        "nodes": len(nodes),
        "edges": len(edge_data),
        "graph": attributes,
        "node_tags": list(NODE_TAGS),
        "edge_tags": list(EDGE_TAGS),
        "node_floats": list(NODE_FLOATS),
        "edge_floats": list(EDGE_FLOATS),
        "encodings": encodings,
        "vocabularies": vocabularies,
    }
    return header, arrays


def write_compact(
    graph: nx.MultiDiGraph, path: Path | str, *, source_fingerprint: str, source_name: str | None = None
) -> dict:
    """Write ``graph`` as a compact file recording the pickle it came from.

    ``source_fingerprint`` is ``file_fingerprint`` of that pickle: pools
    built on it go on validating against this file. Written to a temporary
    name and renamed, so a compact file that exists is whole.
    """

    header, arrays = graph_arrays(graph)
    header["source_fingerprint"] = source_fingerprint
    header["source_name"] = source_name
    header["content_sha256"] = _content_digest(header, arrays)
    path = Path(path)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("wb") as handle:
        np.savez_compressed(
            handle, header=np.frombuffer(json.dumps(header).encode(), dtype=np.uint8), **arrays
        )
    os.replace(temporary, path)
    return header


# --- reading -----------------------------------------------------------------


def _read_file(path: Path | str) -> tuple[dict, dict[str, np.ndarray]]:
    """Header and arrays, checked against the content hash."""

    try:
        with np.load(path, allow_pickle=False) as archive:
            header = json.loads(bytes(archive["header"]))
            arrays = {name: archive[name] for name in archive.files if name != "header"}
    except FileNotFoundError:
        raise
    except (OSError, ValueError, KeyError, EOFError, zipfile.BadZipFile, zlib.error, UnicodeDecodeError) as error:
        raise CompactGraphError(f"{path}: unreadable compact graph ({error})") from None
    if not isinstance(header, dict) or header.get("format") != FORMAT:
        raise CompactGraphError(f"{path}: not a {FORMAT} file")
    if header.get("version") != VERSION:
        raise CompactGraphError(f"{path}: compact format {header.get('version')}, expected {VERSION}")
    if _content_digest(header, arrays) != header.get("content_sha256"):
        raise CompactGraphError(f"{path}: content does not match its recorded hash")
    return header, arrays


def read_compact_header(path: Path | str) -> dict:
    """A compact file's header, after checking its whole content."""

    return _read_file(path)[0]


def _tag_values(vocabulary: list, codes: np.ndarray):
    """(position, value) for every item that has the attribute."""

    positions = np.flatnonzero(codes)
    for position, code in zip(positions.tolist(), codes[positions].tolist()):
        value = vocabulary[code]
        # Each edge gets its own list, as unpickling would give it.
        yield position, list(value) if isinstance(value, list) else value


def _edge_lines(arrays: dict[str, np.ndarray], encodings: dict) -> np.ndarray:
    """One shapely LineString per edge with a geometry, in edge order."""

    import shapely

    coordinates = np.column_stack(
        (_decode_coordinates("geometry_x", arrays, encodings), _decode_coordinates("geometry_y", arrays, encodings))
    )
    offsets = arrays["line_offsets"]
    line = arrays["geometry_line"].astype(np.int64)
    backward = arrays["geometry_reversed"].astype(bool)
    starts, counts = offsets[line], np.diff(offsets)[line]
    edge = np.repeat(np.arange(line.size), counts)
    within = np.arange(edge.size) - np.repeat(np.cumsum(counts) - counts, counts)
    gather = np.where(backward[edge], starts[edge] + counts[edge] - 1 - within, starts[edge] + within)
    return shapely.linestrings(coordinates[gather], indices=edge)


def graph_from_arrays(header: dict, arrays: dict[str, np.ndarray]) -> nx.MultiDiGraph:
    """Rebuild the walk graph, adjacency order included."""

    try:
        encodings, vocabularies = header["encodings"], header["vocabularies"]
        nodes = arrays["node_ids"].tolist()
        xs = _decode_coordinates("node_x", arrays, encodings).tolist()
        ys = _decode_coordinates("node_y", arrays, encodings).tolist()
        node_data = [{"y": y, "x": x} for y, x in zip(ys, xs)]
        for name in header["node_tags"]:
            for position, value in _tag_values(vocabularies[f"node.{name}"], arrays[f"node_tag_{name}"]):
                node_data[position][name] = value
        for name in header["node_floats"]:
            values = arrays[f"node_{name}"]
            present = np.flatnonzero(arrays[f"node_{name}_present"])
            for position, value in zip(present.tolist(), values[present].tolist()):
                node_data[position][name] = value

        edge_count = int(header["edges"])
        edge_data: list[dict] = [{} for _ in range(edge_count)]
        for name in header["edge_tags"]:
            for position, value in _tag_values(vocabularies[f"edge.{name}"], arrays[f"edge_tag_{name}"]):
                edge_data[position][name] = value
        for name in header["edge_floats"]:
            values = arrays[f"edge_{name}"]
            present = np.flatnonzero(arrays[f"edge_{name}_present"])
            for position, value in zip(present.tolist(), values[present].tolist()):
                edge_data[position][name] = value
        with_geometry = np.flatnonzero(arrays["edge_has_geometry"])
        if with_geometry.size:
            for position, line in zip(with_geometry.tolist(), _edge_lines(arrays, encodings).tolist()):
                edge_data[position]["geometry"] = line

        graph = nx.MultiDiGraph()
        graph.graph.update(header["graph"])
        node_store, successors, predecessors = graph._node, graph._succ, graph._pred
        for node, data in zip(nodes, node_data):
            node_store[node] = data
            successors[node] = {}
            predecessors[node] = {}
        targets = arrays["edge_target"].tolist()
        keys = arrays["edge_key"].tolist()
        offsets = arrays["succ_offsets"].tolist()
        if len(offsets) != len(nodes) + 1 or offsets[-1] != edge_count or len(targets) != edge_count:
            raise CompactGraphError("edge arrays do not match the node count")
        for position, node in enumerate(nodes):
            adjacent = successors[node]
            for edge in range(offsets[position], offsets[position + 1]):
                successor = nodes[targets[edge]]
                keydict = adjacent.get(successor)
                if keydict is None:
                    keydict = adjacent[successor] = {}
                keydict[keys[edge]] = edge_data[edge]
        sources = arrays["pred_sources"].tolist()
        pred_offsets = arrays["pred_offsets"].tolist()
        for position, node in enumerate(nodes):
            incoming = predecessors[node]
            for source in sources[pred_offsets[position] : pred_offsets[position + 1]]:
                predecessor = nodes[source]
                # The same key dict as the successor side, as add_edge makes it.
                incoming[predecessor] = successors[predecessor][node]
    except CompactGraphError:
        raise
    except (KeyError, IndexError, TypeError, ValueError) as error:
        raise CompactGraphError(f"malformed compact graph ({error!r})") from None
    return graph


def read_compact(path: Path | str) -> nx.MultiDiGraph:
    header, arrays = _read_file(path)
    try:
        return graph_from_arrays(header, arrays)
    except CompactGraphError as error:
        raise CompactGraphError(f"{path}: {error}") from None


def read_pickle(path: Path | str) -> nx.MultiDiGraph:
    """Unpickle a graph, streaming so the compressed bytes are never all in memory."""

    with gzip.open(path, "rb") as handle:
        return pickle.load(handle)


def read_graph(path: Path | str) -> nx.MultiDiGraph:
    """Load a graph file of either kind; a damaged compact file raises CompactGraphError."""

    return read_compact(path) if is_compact(path) else read_pickle(path)


# --- choosing and fingerprinting ---------------------------------------------


@functools.lru_cache(maxsize=4096)
def _compact_is_current(compact: str, compact_stamp: tuple, source: str, source_stamp: tuple) -> bool:
    """Whether a compact file beside its pickle is intact and made from it.

    Hashes the pickle once per file version and process. A compact file left
    over from an older pickle would otherwise shadow the rebuilt one.
    """

    try:
        header = read_compact_header(compact)
    except (CompactGraphError, OSError) as error:
        LOGGER.warning("serving %s, not its compact file: %s", source, error)
        return False
    fingerprint = file_fingerprint(source)
    if header.get("source_fingerprint") != fingerprint:
        LOGGER.warning(
            "serving %s, not %s: that was made from graph %s, this is %s",
            source, compact, header.get("source_fingerprint"), fingerprint,
        )
        return False
    return True


def _stamp(stat: os.stat_result) -> tuple:
    return (stat.st_mtime_ns, stat.st_size, stat.st_ino)


def graph_file(path: Path | str) -> Path:
    """The file to serve for a graph configured as ``<name>.pkl.gz``.

    The compact twin when it exists and either stands alone or checks out
    against the pickle beside it; otherwise the pickle. The result may not
    exist, which callers report as missing map data.
    """

    path = Path(path)
    if is_compact(path):
        return path
    compact = compact_path(path)
    try:
        compact_stat = compact.stat()
    except OSError:
        return path
    try:
        source_stat = path.stat()
    except OSError:
        return compact
    if _compact_is_current(str(compact), _stamp(compact_stat), str(path), _stamp(source_stat)):
        return compact
    return path


# A recorded hash is 16 hex digits, so this never matches one.
INVALID_FINGERPRINT = "invalid-compact"


def graph_fingerprint(path: Path | str) -> str:
    """The hash pools record for the graph in this file.

    A pickle's is the sha256 of its bytes. A compact file's is the hash of
    the pickle it was made from, provided its own content hash checks out;
    a damaged one gets ``INVALID_FINGERPRINT``, which no pool matches.
    """

    if not is_compact(path):
        return file_fingerprint(path)
    try:
        return str(read_compact_header(path)["source_fingerprint"])
    except (CompactGraphError, KeyError) as error:
        LOGGER.warning("refusing compact graph %s: %s", path, error)
        return INVALID_FINGERPRINT


# --- verification ------------------------------------------------------------


def _same_value(first: Any, second: Any) -> bool:
    if isinstance(first, float) and isinstance(second, float) and math.isnan(first) and math.isnan(second):
        return True
    return type(first) is type(second) and first == second


def _same_number(first: Any, second: Any) -> bool:
    first, second = float(first), float(second)
    return first == second or (math.isnan(first) and math.isnan(second))


def compare_graphs(original: nx.MultiDiGraph, restored: nx.MultiDiGraph, limit: int = 20) -> list[str]:
    """Every difference in what the code reads, between a pickle and its compact graph.

    Node order, successor and predecessor order, edge keys, the kept graph
    attributes, and each kept node and edge attribute: present on both or
    neither, and equal, geometry coordinate for coordinate. An empty list
    means the two plan identically.
    """

    problems: list[str] = []

    def problem(message: str) -> bool:
        problems.append(message)
        return len(problems) >= limit

    if type(original) is not type(restored):
        return [f"graph type {type(original).__name__} != {type(restored).__name__}"]
    kept = {key: value for key, value in original.graph.items() if not str(key).startswith("_runline")}
    if kept != dict(restored.graph):
        if problem(f"graph attributes {kept} != {dict(restored.graph)}"):
            return problems
    if list(original._node) != list(restored._node):
        return problems + ["node ids or their order differ"]
    for node, data in original._node.items():
        other = restored._node[node]
        for name in COORDINATES:
            if not _same_number(data[name], other[name]) or type(other[name]) is not float:
                if problem(f"node {node} {name}: {data[name]!r} != {other[name]!r}"):
                    return problems
        for name in (*NODE_TAGS, *NODE_FLOATS):
            if (name in data) != (name in other):
                if problem(f"node {node} {name}: present {name in data} != {name in other}"):
                    return problems
            elif name in data:
                same = _same_number(data[name], other[name]) if name in NODE_FLOATS else _same_value(data[name], other[name])
                if not same and problem(f"node {node} {name}: {data[name]!r} != {other[name]!r}"):
                    return problems
        if list(original._pred[node]) != list(restored._pred[node]):
            if problem(f"node {node}: predecessor order differs"):
                return problems
        if list(original._succ[node]) != list(restored._succ[node]):
            if problem(f"node {node}: successor order differs"):
                return problems
            continue
        for successor, keydict in original._succ[node].items():
            other_keys = restored._succ[node][successor]
            if list(keydict) != list(other_keys):
                if problem(f"edge {node}->{successor}: keys {list(keydict)} != {list(other_keys)}"):
                    return problems
                continue
            for key, data in keydict.items():
                mine = other_keys[key]
                for name in (*EDGE_TAGS, *EDGE_FLOATS, "geometry"):
                    if (name in data) != (name in mine):
                        if problem(f"edge {node}->{successor}/{key} {name}: present {name in data} != {name in mine}"):
                            return problems
                        continue
                    if name not in data:
                        continue
                    if name == "geometry":
                        same = (
                            getattr(mine[name], "geom_type", None) == data[name].geom_type
                            and np.array_equal(np.asarray(data[name].coords), np.asarray(mine[name].coords))
                        )
                    elif name in EDGE_FLOATS:
                        same = _same_number(data[name], mine[name])
                    else:
                        same = _same_value(data[name], mine[name])
                    if not same and problem(f"edge {node}->{successor}/{key} {name}: {data[name]!r} != {mine[name]!r}"):
                        return problems
    return problems


def convert(source: Path | str, destination: Path | str | None = None, *, verify: bool = False) -> dict:
    """Write the compact twin of a pickled graph; with ``verify``, prove it.

    Returns the header written plus ``problems`` (empty when verified, or
    when not asked to verify). A file that fails verification is removed.
    """

    source = Path(source)
    destination = Path(destination) if destination is not None else compact_path(source)
    payload = source.read_bytes()
    fingerprint = hashlib.sha256(payload).hexdigest()[:16]
    graph = pickle.load(gzip.GzipFile(fileobj=io.BytesIO(payload)))
    del payload
    header = write_compact(graph, destination, source_fingerprint=fingerprint, source_name=source.name)
    problems: list[str] = []
    if verify:
        restored = read_compact(destination)
        problems = compare_graphs(graph, restored)
        if graph_fingerprint(destination) != fingerprint:
            problems.append("recorded source fingerprint does not match the pickle")
        if problems:
            destination.unlink(missing_ok=True)
    return {**header, "problems": problems}
