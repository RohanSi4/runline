"""Binary encoding of precomputed route pools (format 2).

A pool is every distinct loop found from one start cell for one surface. Format
1 stored it as gzipped JSON, which spent most of its bytes on repeated OSM node
ids (about 110 KB per cell for three surfaces). Format 2 stores:

- a node table: the pool's unique OSM node ids, sorted, delta-encoded varints;
- each loop as zigzag-delta varints of indices into that table (consecutive
  nodes along a way usually have neighbouring ids, so most deltas are 1 byte);
- metrics as fixed-point varints;

and compresses the whole thing with lzma. Decoding gives back the format 1
shape, so callers see ``{"origin_node", "origin", "loops": [{"nodes",
"metrics"}]}`` either way. Metrics lose precision below what ranking can see
(0.001 mile, 0.1 ft, 0.001 for fractions).
"""

from __future__ import annotations

import lzma

FORMAT_VERSION = 2

# (field, scale): stored as round(value * scale). Order is the wire order.
METRIC_FIELDS = (
    ("distance_miles", 1000),
    ("elevation_gain_feet", 10),
    ("traffic_signal_events", 1),
    ("major_crossing_events", 1),
    ("trail_fraction", 1000),
    ("repeated_fraction", 1000),
    ("drive_distance_miles", 1000),
    ("elevation_coverage", 1000),
    ("compactness", 1000),
)
COORDINATE_SCALE = 1_000_000


def _write_varint(out: bytearray, value: int) -> None:
    if value < 0:
        raise ValueError("varint must be non-negative")
    while value >= 0x80:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    out.append(value)


def _zigzag(value: int) -> int:
    return value * 2 if value >= 0 else -value * 2 - 1


def _varints(data: bytes):
    """Every varint in ``data``, in order, as an int64 array.

    The whole payload is varints, and each one ends at its first byte below
    0x80, so they can all be read at once. Serving decodes up to nine pools
    per request; reading them a byte at a time in Python took 5.5 ms per
    Charlottesville pool, against 1.4 ms this way.
    """

    import numpy as np

    raw = np.frombuffer(data, dtype=np.uint8)
    ends = np.flatnonzero(raw < 0x80)
    starts = np.concatenate(([0], ends[:-1] + 1))
    place = np.arange(len(raw)) - np.repeat(starts, ends - starts + 1)
    values = (raw & 0x7F).astype(np.uint64) << (7 * place).astype(np.uint64)
    return np.add.reduceat(values, starts).astype(np.int64)


def _unzigzag_array(values):
    return (values >> 1) ^ -(values & 1)


def encode_pool(pool: dict) -> bytes:
    """Pack a pool dict (format 1 shape) into format 2 bytes."""

    table = sorted({node for loop in pool["loops"] for node in loop["nodes"]} | {pool["origin_node"]})
    index = {node: position for position, node in enumerate(table)}
    out = bytearray([FORMAT_VERSION])

    _write_varint(out, len(table))
    previous = 0
    for node in table:
        _write_varint(out, node - previous)
        previous = node

    _write_varint(out, index[pool["origin_node"]])
    latitude, longitude = pool["origin"]
    _write_varint(out, _zigzag(round(latitude * COORDINATE_SCALE)))
    _write_varint(out, _zigzag(round(longitude * COORDINATE_SCALE)))

    _write_varint(out, len(pool["loops"]))
    for loop in pool["loops"]:
        nodes = loop["nodes"]
        _write_varint(out, len(nodes))
        previous = 0
        for node in nodes:
            _write_varint(out, _zigzag(index[node] - previous))
            previous = index[node]
        metrics = loop["metrics"]
        for name, scale in METRIC_FIELDS:
            _write_varint(out, round(metrics[name] * scale))
    return lzma.compress(bytes(out), preset=9 | lzma.PRESET_EXTREME)


def decode_pool(blob: bytes) -> dict:
    """Unpack format 2 bytes into the format 1 pool shape."""

    import numpy as np

    values = _varints(lzma.decompress(blob))
    version = int(values[0])
    if version != FORMAT_VERSION:
        raise ValueError(f"pool format {version}, expected {FORMAT_VERSION}")

    count = int(values[1])
    table = np.cumsum(values[2 : 2 + count])
    position = 2 + count
    origin_node = int(table[values[position]])
    latitude, longitude = _unzigzag_array(values[position + 1 : position + 3]).tolist()
    origin = [latitude / COORDINATE_SCALE, longitude / COORDINATE_SCALE]

    loops = []
    loop_count = int(values[position + 3])
    position += 4
    for _ in range(loop_count):
        length = int(values[position])
        indices = np.cumsum(_unzigzag_array(values[position + 1 : position + 1 + length]))
        nodes = table[indices].tolist()
        position += 1 + length
        stored = values[position : position + len(METRIC_FIELDS)].tolist()
        position += len(METRIC_FIELDS)
        metrics = {name: value if scale == 1 else value / scale for (name, scale), value in zip(METRIC_FIELDS, stored)}
        loops.append({"nodes": nodes, "metrics": metrics})
    return {"origin_node": origin_node, "origin": origin, "loops": loops}
