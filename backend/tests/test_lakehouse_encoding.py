"""Unit tests for the lakehouse column codecs (PLAIN / DICTIONARY / RLE)."""

from __future__ import annotations

import pytest

from app.lakehouse.warehouse.encoding import (
    Encoding,
    choose_encoding,
    decode,
    encode,
    encode_auto,
    read_svarint,
    read_uvarint,
    write_svarint,
    write_uvarint,
)
from app.lakehouse.warehouse.types import ColumnVector, LogicalType

ALL = (Encoding.PLAIN, Encoding.DICTIONARY, Encoding.RLE)


def roundtrip(dtype: LogicalType, items: list) -> None:
    v = ColumnVector.from_pylist(dtype, items)
    for enc in ALL:
        blob = encode(v, enc)
        assert decode(blob, dtype) == v, (dtype, enc)
    auto_enc, auto_blob = encode_auto(v)
    assert decode(auto_blob, dtype) == v
    assert auto_enc is choose_encoding(v)


def test_uvarint_roundtrip() -> None:
    for n in [0, 1, 127, 128, 255, 256, 16383, 16384, 10**18]:
        out = bytearray()
        write_uvarint(out, n)
        got, pos = read_uvarint(bytes(out), 0)
        assert got == n
        assert pos == len(out)


def test_uvarint_rejects_negative() -> None:
    with pytest.raises(ValueError):
        write_uvarint(bytearray(), -1)


def test_svarint_roundtrip_signed() -> None:
    for n in [0, 1, -1, 2, -2, 127, -128, 10**15, -(10**15)]:
        out = bytearray()
        write_svarint(out, n)
        got, _ = read_svarint(bytes(out), 0)
        assert got == n


def test_roundtrip_int64_with_nulls() -> None:
    roundtrip(LogicalType.INT64, [1, -5, None, 1_000_000_000_000, 0, -999])


def test_roundtrip_strings() -> None:
    roundtrip(LogicalType.STRING, ["a", "a", "b", None, "café", "", "c"])


def test_roundtrip_bool() -> None:
    roundtrip(LogicalType.BOOL, [True, False, None, True, True])


def test_roundtrip_float() -> None:
    roundtrip(LogicalType.FLOAT64, [1.5, -2.5, None, 3.0, 0.0])


def test_roundtrip_float32() -> None:
    v = ColumnVector.from_pylist(LogicalType.FLOAT32, [1.0, 2.0, None])
    for enc in ALL:
        assert decode(encode(v, enc), LogicalType.FLOAT32) == v


def test_roundtrip_bytes() -> None:
    roundtrip(LogicalType.BYTES, [b"x", b"x", None, b"yy"])


def test_roundtrip_timestamp() -> None:
    roundtrip(LogicalType.TIMESTAMP, [0, 1_719_600_000_000_000, None, 42])


def test_roundtrip_empty_and_all_null() -> None:
    roundtrip(LogicalType.INT64, [])
    roundtrip(LogicalType.INT64, [None, None, None])


def test_dictionary_wins_low_cardinality() -> None:
    # 100 rows, 2 distinct strings -> dictionary should be chosen or RLE.
    v = ColumnVector.from_pylist(LogicalType.STRING, ["alpha", "beta"] * 50)
    enc = choose_encoding(v)
    assert enc in (Encoding.DICTIONARY, Encoding.RLE)
    assert decode(encode(v, enc), LogicalType.STRING) == v


def test_rle_wins_long_runs() -> None:
    v = ColumnVector.from_pylist(LogicalType.INT64, [7] * 1000)
    assert choose_encoding(v) is Encoding.RLE


def test_plain_ties_win() -> None:
    # A single distinct value of one row: PLAIN should win ties.
    v = ColumnVector.from_pylist(LogicalType.INT64, [5])
    assert choose_encoding(v) is Encoding.PLAIN


def test_encoding_is_deterministic() -> None:
    v = ColumnVector.from_pylist(LogicalType.STRING, ["b", "a", "b", "c", "a"])
    assert encode(v, Encoding.DICTIONARY) == encode(v, Encoding.DICTIONARY)
