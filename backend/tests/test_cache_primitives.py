"""Unit tests for the cache layer primitives — clock, codecs, entry, keys, metrics.

Pure, infra-free, deterministic (the cache uses an injectable clock so nothing
here sleeps or touches real time).
"""

from __future__ import annotations

import math
import random

import pytest

from app.cache.clock import SYSTEM_CLOCK, FakeClock, SystemClock
from app.cache.codecs import BytesCodec, JsonCodec, PickleCodec
from app.cache.entry import CacheEntry
from app.cache.errors import SerializationError
from app.cache.keys import derive_key, fingerprint, qualify
from app.cache.metrics import CacheMetrics

# --------------------------------------------------------------------------- #
# Clock
# --------------------------------------------------------------------------- #


def test_fake_clock_advances_and_is_monotonic() -> None:
    clk = FakeClock(start=1000.0)
    assert clk.time() == 1000.0
    assert clk.monotonic() == 0.0
    clk.advance(5.0)
    assert clk.time() == 1005.0
    assert clk.monotonic() == 5.0


def test_fake_clock_rejects_backwards() -> None:
    clk = FakeClock(start=100.0)
    with pytest.raises(ValueError):
        clk.advance(-1.0)
    with pytest.raises(ValueError):
        clk.set(50.0)


def test_system_clock_is_monotonic_and_real() -> None:
    clk = SystemClock()
    a = clk.monotonic()
    b = clk.monotonic()
    assert b >= a
    assert clk.time() > 0
    assert SYSTEM_CLOCK.time() > 0


# --------------------------------------------------------------------------- #
# Codecs
# --------------------------------------------------------------------------- #


def test_json_codec_roundtrip() -> None:
    codec = JsonCodec()
    value = {"a": 1, "b": [1, 2, 3], "c": "héllo"}
    blob = codec.encode(value)
    assert isinstance(blob, bytes)
    assert codec.decode(blob) == value


def test_json_codec_sort_keys_is_stable() -> None:
    codec = JsonCodec(sort_keys=True)
    a = codec.encode({"b": 1, "a": 2})
    b = codec.encode({"a": 2, "b": 1})
    assert a == b


def test_json_codec_rejects_unencodable() -> None:
    codec = JsonCodec()
    with pytest.raises(SerializationError):
        codec.encode(object())


def test_json_codec_rejects_garbage_decode() -> None:
    codec = JsonCodec()
    with pytest.raises(SerializationError):
        codec.decode(b"\xff\xfe not json")


def test_pickle_codec_roundtrip() -> None:
    codec = PickleCodec()
    value = {"x": (1, 2), "y": {3, 4}}
    assert codec.decode(codec.encode(value)) == value


def test_pickle_codec_rejects_garbage() -> None:
    codec = PickleCodec()
    with pytest.raises(SerializationError):
        codec.decode(b"not a pickle stream")


def test_bytes_codec_bytes_mode() -> None:
    codec = BytesCodec()
    assert codec.decode(codec.encode(b"raw")) == b"raw"
    with pytest.raises(SerializationError):
        codec.encode("a string")


def test_bytes_codec_text_mode() -> None:
    codec = BytesCodec(text=True)
    assert codec.decode(codec.encode("héllo")) == "héllo"
    with pytest.raises(SerializationError):
        codec.encode(b"bytes")


# --------------------------------------------------------------------------- #
# CacheEntry
# --------------------------------------------------------------------------- #


def test_entry_expiry_and_remaining() -> None:
    e = CacheEntry.of("v", now=100.0, ttl=10.0)
    assert not e.is_expired(105.0)
    assert e.is_expired(110.0)
    assert e.remaining(105.0) == pytest.approx(5.0)
    assert e.remaining(200.0) == 0.0
    assert e.age(130.0) == pytest.approx(30.0)


def test_entry_no_ttl_never_expires() -> None:
    e = CacheEntry.of("v", now=0.0, ttl=None)
    assert not e.is_expired(1e9)
    assert e.remaining(1e9) == math.inf
    assert not e.should_early_expire(1e9)


def test_negative_entry_marks_value_absent() -> None:
    e = CacheEntry.of(None, now=0.0, ttl=5.0, negative=True)
    assert e.negative
    # The value slot is the private sentinel, not user None.
    assert e.value is not None


def test_early_expire_certain_past_hard_expiry() -> None:
    e = CacheEntry.of("v", now=0.0, ttl=10.0)
    assert e.should_early_expire(10.0)  # at/after expiry -> always


def test_early_expire_probabilistic_window() -> None:
    # With a seeded RNG and a large beta the entry should sometimes early-expire
    # well before its hard deadline, and never before its creation.
    e = CacheEntry.of("v", now=0.0, ttl=100.0, codec="json")
    rng = random.Random(1234)
    early = sum(
        1 for _ in range(2000) if e.should_early_expire(95.0, beta=50.0, delta=10.0, rng=rng)
    )
    # Some fraction should volunteer for early refresh; not all, not none.
    assert 0 < early < 2000


# --------------------------------------------------------------------------- #
# Keys
# --------------------------------------------------------------------------- #


def test_qualify_joins_namespace() -> None:
    assert qualify("ns", "key") == "ns:key"
    assert qualify("", "key") == "key"


def test_fingerprint_is_stable_and_order_independent_for_dicts() -> None:
    a = fingerprint({"x": 1, "y": 2})
    b = fingerprint({"y": 2, "x": 1})
    assert a == b
    assert fingerprint([1, 2, 3]) != fingerprint([3, 2, 1])


def test_fingerprint_sets_are_order_independent() -> None:
    assert fingerprint({1, 2, 3}) == fingerprint({3, 1, 2})


def test_derive_key_includes_positional_and_filters_kwargs() -> None:
    k1 = derive_key("p", (1,), {"a": 1, "session": "x"})
    k2 = derive_key("p", (1,), {"a": 1, "session": "y"}, exclude=["session"])
    k3 = derive_key("p", (1,), {"a": 1, "session": "z"}, exclude=["session"])
    # Excluding session makes the two differ-only-in-session calls collide.
    assert k2 == k3
    assert k1 != k2


def test_derive_key_include_whitelist() -> None:
    k1 = derive_key("p", (), {"keep": 1, "drop": "a"}, include=["keep"])
    k2 = derive_key("p", (), {"keep": 1, "drop": "b"}, include=["keep"])
    assert k1 == k2


def test_derive_key_long_keys_are_hashed_short() -> None:
    big = "x" * 5000
    key = derive_key("prefix", (big,), {})
    assert len(key) <= 96


# --------------------------------------------------------------------------- #
# Metrics
# --------------------------------------------------------------------------- #


def test_metrics_counts_and_hit_rate() -> None:
    m = CacheMetrics()
    m.inc_hit("ns")
    m.inc_hit("ns")
    m.inc_miss("ns")
    stats = m.stats("ns")
    assert stats.hits == 2
    assert stats.misses == 1
    assert stats.lookups == 3
    assert stats.hit_rate == pytest.approx(2 / 3)
    assert m.hit_rate("ns") == pytest.approx(2 / 3)


def test_metrics_unknown_namespace_is_zero() -> None:
    m = CacheMetrics()
    stats = m.stats("never-seen")
    assert stats.lookups == 0
    assert stats.hit_rate == 0.0


def test_metrics_snapshot_and_reset() -> None:
    m = CacheMetrics()
    m.inc_set("a")
    m.inc_eviction("b")
    snap = m.snapshot()
    assert set(snap) == {"a", "b"}
    assert snap["a"].sets == 1
    assert snap["b"].evictions == 1
    m.reset("a")
    assert m.stats("a").sets == 0
    assert m.stats("b").evictions == 1
    m.reset()
    assert m.snapshot() == {}


def test_metrics_as_dict_shape() -> None:
    m = CacheMetrics()
    m.inc_hit("ns")
    d = m.stats("ns").as_dict()
    assert d["namespace"] == "ns"
    assert d["hits"] == 1
    assert "hit_rate" in d
