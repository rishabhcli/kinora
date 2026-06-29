"""Unit tests for the dependency-precise query-result cache (no infra)."""

from __future__ import annotations

import pytest

from app.datascale.optimize.errors import CacheError
from app.datascale.optimize.resultcache import (
    ResultCache,
    RowScope,
    make_cache_key,
)


def test_key_is_param_sensitive_but_literal_insensitive() -> None:
    # Same shape, different bound params -> different keys.
    k1 = make_cache_key("SELECT * FROM book WHERE id = $1", {"id": 1})
    k2 = make_cache_key("SELECT * FROM book WHERE id = $1", {"id": 2})
    assert k1 != k2
    # Same shape + same params, different literal text -> same key.
    k3 = make_cache_key("select * from book where id = 99", {"id": 1})
    assert k1 == k3


def test_param_order_independent_for_dicts() -> None:
    a = make_cache_key("SELECT 1 FROM t WHERE a = :a AND b = :b", {"a": 1, "b": 2})
    b = make_cache_key("SELECT 1 FROM t WHERE a = :a AND b = :b", {"b": 2, "a": 1})
    assert a == b


def test_basic_hit_miss() -> None:
    cache: ResultCache[str] = ResultCache()
    assert cache.get("SELECT * FROM book", None) is None
    assert cache.stats.misses == 1
    cache.put("SELECT * FROM book", "ROWS", dependencies=["book"])
    assert cache.get("SELECT * FROM book", None) == "ROWS"
    assert cache.stats.hits == 1


def test_get_or_compute() -> None:
    cache: ResultCache[int] = ResultCache()
    calls = []

    def compute() -> int:
        calls.append(1)
        return 42

    assert cache.get_or_compute("SELECT 1 FROM t", compute, dependencies=["t"]) == 42
    assert cache.get_or_compute("SELECT 1 FROM t", compute, dependencies=["t"]) == 42
    assert len(calls) == 1  # computed once


def test_ttl_expiry() -> None:
    now = [100.0]
    cache: ResultCache[str] = ResultCache(now=lambda: now[0])
    cache.put("SELECT * FROM t", "V", dependencies=["t"], ttl_s=10)
    assert cache.get("SELECT * FROM t") == "V"
    now[0] += 11
    assert cache.get("SELECT * FROM t") is None
    assert cache.stats.expirations == 1


def test_default_ttl_applies() -> None:
    now = [0.0]
    cache: ResultCache[str] = ResultCache(default_ttl_s=5, now=lambda: now[0])
    cache.put("SELECT * FROM t", "V", dependencies=["t"])
    now[0] += 6
    assert cache.get("SELECT * FROM t") is None


def test_explicit_none_ttl_never_expires() -> None:
    now = [0.0]
    cache: ResultCache[str] = ResultCache(default_ttl_s=5, now=lambda: now[0])
    cache.put("SELECT * FROM t", "V", dependencies=["t"], ttl_s=None)
    now[0] += 1000
    assert cache.get("SELECT * FROM t") == "V"


def test_lru_eviction() -> None:
    cache: ResultCache[int] = ResultCache(capacity=2)
    cache.put("SELECT 1 FROM a", 1, dependencies=["a"])
    cache.put("SELECT 1 FROM b", 2, dependencies=["b"])
    # Touch a so it is most-recently-used.
    assert cache.get("SELECT 1 FROM a") == 1
    cache.put("SELECT 1 FROM c", 3, dependencies=["c"])  # evicts b (LRU)
    assert cache.get("SELECT 1 FROM b") is None
    assert cache.get("SELECT 1 FROM a") == 1
    assert cache.get("SELECT 1 FROM c") == 3
    assert cache.stats.evictions == 1


def test_invalidate_table_is_precise() -> None:
    cache: ResultCache[str] = ResultCache()
    cache.put("SELECT * FROM book", "books", dependencies=["book"])
    cache.put("SELECT * FROM shot", "shots", dependencies=["shot"])
    removed = cache.invalidate_table("book")
    assert removed == 1
    assert cache.get("SELECT * FROM book") is None
    assert cache.get("SELECT * FROM shot") == "shots"  # untouched


def test_invalidate_multi_table_entry() -> None:
    cache: ResultCache[str] = ResultCache()
    # A join depends on both tables -> a write to either invalidates it.
    cache.put(
        "SELECT * FROM book b JOIN shot s ON s.book_id = b.id",
        "joined",
        dependencies=["book", "shot"],
    )
    assert cache.invalidate_table("shot") == 1
    assert cache.get("SELECT * FROM book b JOIN shot s ON s.book_id = b.id") is None


def test_invalidate_tables_bulk() -> None:
    cache: ResultCache[str] = ResultCache()
    cache.put("SELECT * FROM a", "1", dependencies=["a"])
    cache.put("SELECT * FROM b", "2", dependencies=["b"])
    cache.put("SELECT * FROM c", "3", dependencies=["c"])
    assert cache.invalidate_tables(["a", "c"]) == 2
    assert len(cache) == 1


def test_row_scoped_invalidation_keeps_other_rows_hot() -> None:
    cache: ResultCache[str] = ResultCache()
    # Two per-book queries, each row-scoped to its own book_id.
    cache.put(
        "SELECT * FROM shot WHERE book_id = $1",
        "book7",
        params={"id": 7},
        dependencies=["shot"],
        row_scopes=[RowScope("shot", "book_id", 7)],
    )
    cache.put(
        "SELECT * FROM shot WHERE book_id = $1",
        "book9",
        params={"id": 9},
        dependencies=["shot"],
        row_scopes=[RowScope("shot", "book_id", 9)],
    )
    # A write to book 7 invalidates only the book-7 entry.
    removed = cache.invalidate_write("shot", row_scopes=[RowScope("shot", "book_id", 7)])
    assert removed == 1
    assert cache.get("SELECT * FROM shot WHERE book_id = $1", {"id": 7}) is None
    assert cache.get("SELECT * FROM shot WHERE book_id = $1", {"id": 9}) == "book9"


def test_row_scoped_write_also_drops_unscoped_table_entries() -> None:
    cache: ResultCache[str] = ResultCache()
    # An unscoped table-wide aggregate cannot be proven to exclude the written row.
    cache.put("SELECT count(*) FROM shot", "agg", dependencies=["shot"])
    cache.put(
        "SELECT * FROM shot WHERE book_id = $1",
        "book9",
        params={"id": 9},
        dependencies=["shot"],
        row_scopes=[RowScope("shot", "book_id", 9)],
    )
    removed = cache.invalidate_write("shot", row_scopes=[RowScope("shot", "book_id", 7)])
    # Only the unscoped aggregate is dropped; the book-9 scoped entry survives.
    assert removed == 1
    assert cache.get("SELECT count(*) FROM shot") is None
    assert cache.get("SELECT * FROM shot WHERE book_id = $1", {"id": 9}) == "book9"


def test_invalidate_write_without_scopes_is_coarse() -> None:
    cache: ResultCache[str] = ResultCache()
    cache.put(
        "SELECT * FROM shot WHERE book_id = $1",
        "book9",
        params={"id": 9},
        dependencies=["shot"],
        row_scopes=[RowScope("shot", "book_id", 9)],
    )
    # No row scopes on the write -> drop everything depending on the table.
    assert cache.invalidate_write("shot") == 1


def test_invalidate_row_directly() -> None:
    cache: ResultCache[str] = ResultCache()
    cache.put(
        "SELECT * FROM book WHERE id = $1",
        "b1",
        params={"id": 1},
        dependencies=["book"],
        row_scopes=[RowScope("book", "id", 1)],
    )
    assert cache.invalidate_row(RowScope("book", "id", 1)) == 1
    assert len(cache) == 0


def test_put_replaces_and_cleans_index() -> None:
    cache: ResultCache[str] = ResultCache()
    cache.put("SELECT * FROM a", "1", dependencies=["a"])
    cache.put("SELECT * FROM a", "2", dependencies=["b"])  # same key, new deps
    assert cache.get("SELECT * FROM a") == "2"
    # The old 'a' dependency link must be gone.
    assert cache.invalidate_table("a") == 0
    assert cache.invalidate_table("b") == 1


def test_sweep_expired() -> None:
    now = [0.0]
    cache: ResultCache[str] = ResultCache(now=lambda: now[0])
    cache.put("SELECT * FROM a", "1", dependencies=["a"], ttl_s=5)
    cache.put("SELECT * FROM b", "2", dependencies=["b"], ttl_s=None)
    now[0] += 6
    assert cache.sweep_expired() == 1
    assert len(cache) == 1


def test_clear() -> None:
    cache: ResultCache[str] = ResultCache()
    cache.put("SELECT * FROM a", "1", dependencies=["a"])
    cache.clear()
    assert len(cache) == 0
    assert cache.invalidate_table("a") == 0


def test_stats_hit_rate() -> None:
    cache: ResultCache[str] = ResultCache()
    cache.put("SELECT * FROM a", "1", dependencies=["a"])
    cache.get("SELECT * FROM a")  # hit
    cache.get("SELECT * FROM b")  # miss
    assert cache.stats.hit_rate == 0.5
    assert cache.stats.as_dict()["hits"] == 1


def test_unserializable_params_raise() -> None:
    cache: ResultCache[str] = ResultCache()

    class Bad:
        __slots__ = ()  # str() works, but make it explicitly non-json

    # An object with a circular reference defeats json + default=str fallback.
    circular: dict[str, object] = {}
    circular["self"] = circular
    with pytest.raises(CacheError):
        cache.put("SELECT 1 FROM t", "v", params=circular, dependencies=["t"])


def test_bad_capacity_raises() -> None:
    with pytest.raises(CacheError):
        ResultCache(capacity=0)
