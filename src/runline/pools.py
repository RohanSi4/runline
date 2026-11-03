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


def _unzigzag(value: int) -> int:
    return value >> 1 if not value & 1 else -((value + 1) >> 1)


class _Reader:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.position = 0

    def varint(self) -> int:
        result = shift = 0
        while True:
            byte = self.data[self.position]
            self.position += 1
            result |= (byte & 0x7F) << shift
            if byte < 0x80:
                return result
            shift += 7

    def signed(self) -> int:
        return _unzigzag(self.varint())


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

    reader = _Reader(lzma.decompress(blob))
    version = reader.varint()
    if version != FORMAT_VERSION:
        raise ValueError(f"pool format {version}, expected {FORMAT_VERSION}")

    table = []
    node = 0
    for _ in range(reader.varint()):
        node += reader.varint()
        table.append(node)

    origin_node = table[reader.varint()]
    origin = [reader.signed() / COORDINATE_SCALE, reader.signed() / COORDINATE_SCALE]

    loops = []
    for _ in range(reader.varint()):
        position = 0
        nodes = []
        for _ in range(reader.varint()):
            position += reader.signed()
            nodes.append(table[position])
        metrics = {}
        for name, scale in METRIC_FIELDS:
            value = reader.varint()
            metrics[name] = value if scale == 1 else value / scale
        loops.append({"nodes": nodes, "metrics": metrics})
    return {"origin_node": origin_node, "origin": origin, "loops": loops}
