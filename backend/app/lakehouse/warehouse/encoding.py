"""Physical column codecs: PLAIN, DICTIONARY, and RLE.

Each codec serialises a :class:`~app.lakehouse.warehouse.types.ColumnVector` to a
deterministic ``bytes`` blob and reconstructs the *exact* same vector (values +
null bitmap). The format is hand-rolled (stdlib ``struct`` only) so the warehouse
carries no heavy serialisation dependency.

Layout
------
Every encoded blob starts with a one-byte :class:`Encoding` tag, then the row
count (uvarint), then the null bitmap (uvarint length + packed bits), then the
codec-specific payload over the **non-null** logical values only:

* **PLAIN** — each value length-prefixed/packed by type, in order.
* **DICTIONARY** — a sorted, de-duplicated value table (PLAIN-encoded) followed by
  one uvarint dictionary-index per non-null value. Wins on low-cardinality columns.
* **RLE** — ``(value, run_length)`` pairs over the non-null stream, the value
  PLAIN-encoded and the run length a uvarint. Wins on long sorted/repeated runs.

The bitmap is stored once, codec-independent; payloads only ever see present
values, so a column of mostly-nulls still round-trips exactly.

A :func:`choose_encoding` heuristic picks the smallest of the three for a given
vector (it actually encodes all candidates and measures — deterministic and exact,
which matters more than speed at warehouse-write time).
"""

from __future__ import annotations

import enum
import struct
from typing import Any

from app.lakehouse.warehouse.types import ColumnVector, LogicalType, placeholder_for


class Encoding(enum.IntEnum):
    """On-disk codec tag (first byte of every encoded column blob)."""

    PLAIN = 1
    DICTIONARY = 2
    RLE = 3


# --------------------------------------------------------------------------- #
# Primitive (de)serialisation helpers — uvarint + per-type value codecs.
# --------------------------------------------------------------------------- #


def write_uvarint(out: bytearray, value: int) -> None:
    """LEB128 unsigned varint."""
    if value < 0:
        raise ValueError("uvarint cannot encode a negative value")
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return


def read_uvarint(buf: bytes, pos: int) -> tuple[int, int]:
    """Return ``(value, new_pos)`` reading a uvarint at ``pos``."""
    result = 0
    shift = 0
    while True:
        byte = buf[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
        shift += 7


def write_svarint(out: bytearray, value: int) -> None:
    """Signed varint via zig-zag (so small magnitudes stay short for negatives).

    Zig-zag maps ``0,-1,1,-2,2,...`` → ``0,1,2,3,4,...`` so the magnitude of a small
    negative integer encodes in as few bytes as the matching positive one. Works for
    arbitrary-width Python ints (no 64-bit truncation).
    """
    # zig-zag: non-negative n -> 2n; negative n -> -2n-1. Bijective onto N for any
    # arbitrary-width int (no fixed-width assumption).
    write_uvarint(out, value << 1 if value >= 0 else (-value << 1) - 1)


def read_svarint(buf: bytes, pos: int) -> tuple[int, int]:
    raw, pos = read_uvarint(buf, pos)
    # Inverse zig-zag: even -> raw//2; odd -> -(raw+1)//2.
    return (raw >> 1) if raw & 1 == 0 else -((raw + 1) >> 1), pos


# Logical types physically stored as a signed varint.
_VARINT_TYPES = (
    LogicalType.INT32,
    LogicalType.INT64,
    LogicalType.TIMESTAMP,
    LogicalType.DECIMAL,
)


def _write_value(out: bytearray, dtype: LogicalType, value: Any) -> None:
    if dtype is LogicalType.BOOL:
        out.append(1 if value else 0)
    elif dtype in _VARINT_TYPES:
        write_svarint(out, int(value))
    elif dtype is LogicalType.FLOAT32:
        out.extend(struct.pack("<f", float(value)))
    elif dtype is LogicalType.FLOAT64:
        out.extend(struct.pack("<d", float(value)))
    elif dtype is LogicalType.STRING:
        raw = value.encode("utf-8")
        write_uvarint(out, len(raw))
        out.extend(raw)
    elif dtype is LogicalType.BYTES:
        write_uvarint(out, len(value))
        out.extend(value)
    else:  # pragma: no cover - exhaustive
        raise ValueError(f"unencodable dtype {dtype}")


def _read_value(buf: bytes, pos: int, dtype: LogicalType) -> tuple[Any, int]:
    if dtype is LogicalType.BOOL:
        return bool(buf[pos]), pos + 1
    if dtype in _VARINT_TYPES:
        return read_svarint(buf, pos)
    if dtype is LogicalType.FLOAT32:
        return struct.unpack_from("<f", buf, pos)[0], pos + 4
    if dtype is LogicalType.FLOAT64:
        return struct.unpack_from("<d", buf, pos)[0], pos + 8
    if dtype is LogicalType.STRING:
        n, pos = read_uvarint(buf, pos)
        return buf[pos : pos + n].decode("utf-8"), pos + n
    if dtype is LogicalType.BYTES:
        n, pos = read_uvarint(buf, pos)
        return bytes(buf[pos : pos + n]), pos + n
    raise ValueError(f"undecodable dtype {dtype}")  # pragma: no cover


def _pack_bitmap(valid: list[bool]) -> bytes:
    """LSB-first packed bits; length-prefixed by the caller."""
    out = bytearray()
    acc = 0
    nbits = 0
    for bit in valid:
        if bit:
            acc |= 1 << nbits
        nbits += 1
        if nbits == 8:
            out.append(acc)
            acc = 0
            nbits = 0
    if nbits:
        out.append(acc)
    return bytes(out)


def _unpack_bitmap(buf: bytes, pos: int, count: int) -> tuple[list[bool], int]:
    nbytes = (count + 7) // 8
    raw = buf[pos : pos + nbytes]
    bits = [bool((raw[i // 8] >> (i % 8)) & 1) for i in range(count)]
    return bits, pos + nbytes


# --------------------------------------------------------------------------- #
# Encoders / decoders.
# --------------------------------------------------------------------------- #


def _present_values(vec: ColumnVector) -> list[Any]:
    return [vec.value(i) for i in range(len(vec)) if vec.is_valid(i)]


def _frame_header(out: bytearray, tag: Encoding, vec: ColumnVector) -> None:
    out.append(int(tag))
    write_uvarint(out, len(vec))
    bitmap = _pack_bitmap(vec.valid)
    write_uvarint(out, len(bitmap))
    out.extend(bitmap)


def _read_header(buf: bytes) -> tuple[Encoding, int, list[bool], int]:
    tag = Encoding(buf[0])
    count, pos = read_uvarint(buf, 1)
    bm_len, pos = read_uvarint(buf, pos)
    valid, _ = _unpack_bitmap(buf, pos, count)
    pos += bm_len
    return tag, count, valid, pos


def encode_plain(vec: ColumnVector) -> bytes:
    out = bytearray()
    _frame_header(out, Encoding.PLAIN, vec)
    for v in _present_values(vec):
        _write_value(out, vec.dtype, v)
    return bytes(out)


def encode_dictionary(vec: ColumnVector) -> bytes:
    present = _present_values(vec)
    # Deterministic dictionary: sorted unique values.
    uniques = sorted(set(present), key=_sort_key(vec.dtype))
    index_of = {v: i for i, v in enumerate(uniques)}
    out = bytearray()
    _frame_header(out, Encoding.DICTIONARY, vec)
    write_uvarint(out, len(uniques))
    for u in uniques:
        _write_value(out, vec.dtype, u)
    for v in present:
        write_uvarint(out, index_of[v])
    return bytes(out)


def encode_rle(vec: ColumnVector) -> bytes:
    present = _present_values(vec)
    out = bytearray()
    _frame_header(out, Encoding.RLE, vec)
    runs: list[tuple[Any, int]] = []
    for v in present:
        if runs and runs[-1][0] == v:
            runs[-1] = (v, runs[-1][1] + 1)
        else:
            runs.append((v, 1))
    write_uvarint(out, len(runs))
    for value, length in runs:
        _write_value(out, vec.dtype, value)
        write_uvarint(out, length)
    return bytes(out)


_ENCODERS = {
    Encoding.PLAIN: encode_plain,
    Encoding.DICTIONARY: encode_dictionary,
    Encoding.RLE: encode_rle,
}


def encode(vec: ColumnVector, encoding: Encoding) -> bytes:
    return _ENCODERS[encoding](vec)


def decode(buf: bytes, dtype: LogicalType) -> ColumnVector:
    """Reconstruct the exact vector from any codec blob."""
    tag, count, valid, pos = _read_header(buf)
    n_present = sum(valid)
    if tag is Encoding.PLAIN:
        present: list[Any] = []
        for _ in range(n_present):
            value, pos = _read_value(buf, pos, dtype)
            present.append(value)
    elif tag is Encoding.DICTIONARY:
        n_dict, pos = read_uvarint(buf, pos)
        dictionary: list[Any] = []
        for _ in range(n_dict):
            value, pos = _read_value(buf, pos, dtype)
            dictionary.append(value)
        present = []
        for _ in range(n_present):
            idx, pos = read_uvarint(buf, pos)
            present.append(dictionary[idx])
    elif tag is Encoding.RLE:
        n_runs, pos = read_uvarint(buf, pos)
        present = []
        for _ in range(n_runs):
            value, pos = _read_value(buf, pos, dtype)
            length, pos = read_uvarint(buf, pos)
            present.extend([value] * length)
    else:  # pragma: no cover - exhaustive
        raise ValueError(f"unknown encoding {tag}")

    ph = placeholder_for(dtype)
    values: list[Any] = []
    it = iter(present)
    for ok in valid:
        values.append(next(it) if ok else ph)
    return ColumnVector(dtype, values, valid)


def _sort_key(dtype: LogicalType) -> Any:
    """A total-order key for dictionary determinism.

    ``bool`` sorts ``False < True``; everything else uses natural ordering. Bytes
    and strings are already totally ordered.
    """
    if dtype is LogicalType.BOOL:
        return lambda v: 1 if v else 0
    return lambda v: v


def choose_encoding(vec: ColumnVector) -> Encoding:
    """Pick the codec yielding the smallest blob for ``vec`` (ties → PLAIN).

    Deterministic: encodes all three candidates and measures. PLAIN wins ties so a
    column with no benefit stays in the simplest representation.
    """
    sizes = {
        Encoding.PLAIN: len(encode_plain(vec)),
        Encoding.DICTIONARY: len(encode_dictionary(vec)),
        Encoding.RLE: len(encode_rle(vec)),
    }
    best = Encoding.PLAIN
    best_size = sizes[Encoding.PLAIN]
    for enc in (Encoding.DICTIONARY, Encoding.RLE):
        if sizes[enc] < best_size:
            best, best_size = enc, sizes[enc]
    return best


def encode_auto(vec: ColumnVector) -> tuple[Encoding, bytes]:
    """Choose the best codec and return ``(encoding, blob)``."""
    enc = choose_encoding(vec)
    return enc, encode(vec, enc)


__all__ = [
    "Encoding",
    "choose_encoding",
    "decode",
    "encode",
    "encode_auto",
    "encode_dictionary",
    "encode_plain",
    "encode_rle",
    "read_svarint",
    "read_uvarint",
    "write_svarint",
    "write_uvarint",
]
