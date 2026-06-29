"""Unit tests for the shard-key value object (no infra)."""

from __future__ import annotations

import pytest

from app.datascale.sharding.keys import ShardKey, coerce_key


def test_single_component_key_encodes_stably() -> None:
    k1 = ShardKey.of("book-123")
    k2 = ShardKey.of("book-123")
    assert k1 == k2
    assert k1.encode() == k2.encode()
    assert k1.hashed() == k2.hashed()


def test_compound_key_is_order_independent() -> None:
    a = ShardKey.compound(book_id="b1", user_id="u1")
    b = ShardKey.compound(user_id="u1", book_id="b1")
    assert a == b
    assert a.encode() == b.encode()
    assert a.hashed() == b.hashed()


def test_from_mapping_matches_compound() -> None:
    a = ShardKey.compound(book_id="b1", user_id="u1")
    b = ShardKey.from_mapping({"user_id": "u1", "book_id": "b1"})
    assert a == b


def test_component_boundary_is_unambiguous() -> None:
    # "1"+"2" must not collide with "12" — the unit separator prevents it.
    a = ShardKey.compound(x="1", y="2")
    b = ShardKey.compound(x="12", y="")
    assert a.encode() != b.encode()


def test_bool_distinct_from_int() -> None:
    assert ShardKey.of(True).encode() != ShardKey.of(1).encode()
    assert ShardKey.of(False).encode() != ShardKey.of(0).encode()


def test_zero_and_empty_string_are_valid() -> None:
    assert ShardKey.of(0).hashed() >= 0
    assert ShardKey.of("").hashed() >= 0


def test_none_component_rejected() -> None:
    with pytest.raises(ValueError, match="None"):
        ShardKey(components=(("k", None),))  # type: ignore[arg-type]


def test_empty_key_rejected() -> None:
    with pytest.raises(ValueError, match="at least one component"):
        ShardKey(components=())


def test_duplicate_component_rejected() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        ShardKey(components=(("k", "a"), ("k", "b")))


def test_hashed_mod_in_range() -> None:
    k = ShardKey.of("book-xyz")
    for modulus in (1, 2, 7, 64, 1024):
        assert 0 <= k.hashed_mod(modulus) < modulus


def test_hashed_mod_rejects_nonpositive() -> None:
    with pytest.raises(ValueError):
        ShardKey.of("x").hashed_mod(0)


def test_single_value_on_compound_raises() -> None:
    with pytest.raises(ValueError, match="one-component"):
        _ = ShardKey.compound(a="1", b="2").single_value


def test_single_value_returns_lone_component() -> None:
    assert ShardKey.of("b1").single_value == "b1"


def test_coerce_key_passthrough_and_scalar_and_mapping() -> None:
    k = ShardKey.of("b1")
    assert coerce_key(k) is k
    assert coerce_key("b1") == k
    assert coerce_key({"key": "b1"}) == k


def test_coerce_key_rejects_unknown_type() -> None:
    with pytest.raises(TypeError):
        coerce_key(3.14)  # type: ignore[arg-type]


def test_bytes_component_preserved() -> None:
    raw = b"\x00\x01\x02"
    assert ShardKey.of(raw).encode().endswith(raw)


def test_hash_is_process_stable_value() -> None:
    # A golden value pins the encoding so an accidental change is caught.
    k = ShardKey.of("book-123", name="book_id")
    assert k.hashed_mod(1000) == k.hashed_mod(1000)  # idempotent
    # sha1 of "book_id\x1fbook-123" is deterministic.
    import hashlib

    expected = int.from_bytes(
        hashlib.sha1(b"book_id\x1fbook-123").digest(), "big"
    )
    assert k.hashed() == expected
