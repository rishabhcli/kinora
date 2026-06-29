"""Deterministic encryption + blind-index correctness (fixed keys, no infra)."""

from __future__ import annotations

import pytest

from app.zerotrust.crypto import blind_index as bidx
from app.zerotrust.crypto import deterministic as det
from app.zerotrust.crypto.errors import DecryptionError

ENC_KEY = bytes([0x11]) * 32
SIV_KEY = bytes([0x22]) * 32
IDX_KEY = bytes([0x33]) * 32


# --------------------------------------------------------------------------- #
# Deterministic (searchable) encryption
# --------------------------------------------------------------------------- #


def test_deterministic_equal_plaintext_equal_ciphertext() -> None:
    a = det.encrypt_deterministic(ENC_KEY, SIV_KEY, b"alice@example.com")
    b = det.encrypt_deterministic(ENC_KEY, SIV_KEY, b"alice@example.com")
    assert a == b


def test_deterministic_distinct_plaintext_distinct_ciphertext() -> None:
    a = det.encrypt_deterministic(ENC_KEY, SIV_KEY, b"alice@example.com")
    b = det.encrypt_deterministic(ENC_KEY, SIV_KEY, b"bob@example.com")
    assert a != b


def test_deterministic_round_trip() -> None:
    blob = det.encrypt_deterministic(ENC_KEY, SIV_KEY, b"value")
    assert det.decrypt_deterministic(ENC_KEY, blob) == b"value"


def test_deterministic_aad_scopes_equality() -> None:
    # Same plaintext in two AAD contexts must not collide (column scoping).
    a = det.encrypt_deterministic(ENC_KEY, SIV_KEY, b"x", aad=b"col:email")
    b = det.encrypt_deterministic(ENC_KEY, SIV_KEY, b"x", aad=b"col:phone")
    assert a != b
    assert det.decrypt_deterministic(ENC_KEY, a, aad=b"col:email") == b"x"


def test_deterministic_wrong_aad_fails() -> None:
    blob = det.encrypt_deterministic(ENC_KEY, SIV_KEY, b"x", aad=b"col:email")
    with pytest.raises(DecryptionError):
        det.decrypt_deterministic(ENC_KEY, blob, aad=b"col:phone")


def test_deterministic_distinct_keys_distinct_output() -> None:
    a = det.encrypt_deterministic(ENC_KEY, SIV_KEY, b"x")
    b = det.encrypt_deterministic(bytes([0x99]) * 32, SIV_KEY, b"x")
    assert a != b


# --------------------------------------------------------------------------- #
# Blind indexes — equality
# --------------------------------------------------------------------------- #


def test_equality_index_stable_and_keyed() -> None:
    a = bidx.equality_index(IDX_KEY, b"alice")
    assert a == bidx.equality_index(IDX_KEY, b"alice")
    assert a != bidx.equality_index(bytes([0x44]) * 32, b"alice")  # keyed
    assert a != bidx.equality_index(IDX_KEY, b"bob")
    assert len(a) == bidx.TOKEN_BYTES


# --------------------------------------------------------------------------- #
# Blind indexes — prefix
# --------------------------------------------------------------------------- #


def test_prefix_indexes_cover_all_prefixes() -> None:
    tokens = bidx.prefix_indexes(IDX_KEY, b"abcd", max_len=8)
    # one token per prefix length 1..4
    assert len(tokens) == 4
    # the 'abc' probe matches the stored 3-char prefix token
    assert bidx.prefix_query_token(IDX_KEY, b"abc") in tokens
    # a non-prefix does not
    assert bidx.prefix_query_token(IDX_KEY, b"xyz") not in tokens


def test_prefix_respects_max_len() -> None:
    tokens = bidx.prefix_indexes(IDX_KEY, b"abcdefghij", max_len=4)
    assert len(tokens) == 4  # capped


def test_prefix_min_len_validation() -> None:
    with pytest.raises(ValueError):
        bidx.prefix_indexes(IDX_KEY, b"abc", min_len=0)


def test_prefix_token_domain_separated_from_equality() -> None:
    # An equality token of "ab" must not equal the prefix token of "ab".
    assert bidx.equality_index(IDX_KEY, b"ab") != bidx.prefix_query_token(IDX_KEY, b"ab")


# --------------------------------------------------------------------------- #
# Blind indexes — range buckets
# --------------------------------------------------------------------------- #


def test_range_buckets_group_contiguous_values() -> None:
    # 10..19 share a bucket under size 10.
    b10 = bidx.range_buckets(IDX_KEY, 10, bucket_size=10)
    b19 = bidx.range_buckets(IDX_KEY, 19, bucket_size=10)
    b20 = bidx.range_buckets(IDX_KEY, 20, bucket_size=10)
    assert b10 == b19
    assert b10 != b20


def test_buckets_for_range_spans_correctly() -> None:
    buckets = bidx.buckets_for_range(IDX_KEY, 5, 25, bucket_size=10)
    # buckets 0,1,2 -> 3 distinct tokens
    assert len(buckets) == 3
    assert bidx.range_buckets(IDX_KEY, 7, bucket_size=10) in buckets
    assert bidx.range_buckets(IDX_KEY, 23, bucket_size=10) in buckets


def test_buckets_for_range_empty_when_hi_lt_lo() -> None:
    assert bidx.buckets_for_range(IDX_KEY, 10, 5, bucket_size=10) == []


def test_buckets_for_range_guards_explosion() -> None:
    with pytest.raises(ValueError):
        bidx.buckets_for_range(IDX_KEY, 0, 10_000_000, bucket_size=1, max_buckets=100)


def test_range_bucket_size_validation() -> None:
    with pytest.raises(ValueError):
        bidx.range_buckets(IDX_KEY, 1, bucket_size=0)
