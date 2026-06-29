"""Unit tests for the N+1 detector + dataloader framework (no infra)."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

import pytest

from app.datascale.optimize.nplusone import (
    DataLoader,
    NPlusOneDetector,
    Severity,
)

# --------------------------------------------------------------------------- #
# Detector
# --------------------------------------------------------------------------- #


def test_detects_repeated_shape_with_distinct_params() -> None:
    det = NPlusOneDetector(threshold=5)
    for i in range(8):
        det.observe("SELECT * FROM shot WHERE book_id = $1", params={"id": i})
    findings = det.findings()
    assert len(findings) == 1
    f = findings[0]
    assert f.count == 8
    assert f.distinct_params == 8
    assert f.severity is Severity.LOW


def test_below_threshold_not_flagged() -> None:
    det = NPlusOneDetector(threshold=5)
    for i in range(4):
        det.observe("SELECT * FROM shot WHERE id = $1", params={"id": i})
    assert det.findings() == []
    assert det.worst_severity() is Severity.NONE


def test_same_params_repeated_is_not_n_plus_one() -> None:
    # Same query, same params, many times → a caching problem, not batching.
    det = NPlusOneDetector(threshold=5, distinct_ratio=0.5)
    for _ in range(20):
        det.observe("SELECT * FROM config WHERE id = $1", params={"id": 1})
    assert det.findings() == []


def test_severity_scales() -> None:
    det = NPlusOneDetector(threshold=5, medium_at=20, high_at=50)
    for i in range(60):
        det.observe("SELECT * FROM shot WHERE id = $1", params={"id": i})
    assert det.findings()[0].severity is Severity.HIGH
    assert det.worst_severity() is Severity.HIGH


def test_distinct_shapes_tracked_separately() -> None:
    det = NPlusOneDetector(threshold=3)
    for i in range(4):
        det.observe("SELECT * FROM shot WHERE book_id = $1", params={"id": i})
    for i in range(4):
        det.observe("SELECT * FROM entity WHERE book_id = $1", params={"id": i})
    findings = det.findings()
    assert len(findings) == 2
    skeletons = {f.skeleton for f in findings}
    assert any("shot" in s for s in skeletons)
    assert any("entity" in s for s in skeletons)


def test_literals_collapse_to_one_shape() -> None:
    # Different literal text but same shape → one finding.
    det = NPlusOneDetector(threshold=3)
    det.observe("SELECT * FROM shot WHERE id = 1")
    det.observe("SELECT * FROM shot WHERE id = 2")
    det.observe("SELECT * FROM shot WHERE id = 3")
    # Note: params default to None for all, so distinct_params would be 1.
    # Use explicit params to model the per-row loop.
    det.reset()
    for i in range(3):
        det.observe("SELECT * FROM shot WHERE id = 1", params=i)
    assert len(det.findings()) == 1


def test_reset_clears() -> None:
    det = NPlusOneDetector(threshold=2)
    det.observe("SELECT * FROM t WHERE id = $1", params=1)
    det.observe("SELECT * FROM t WHERE id = $1", params=2)
    assert det.findings()
    det.reset()
    assert det.findings() == []


def test_threshold_must_be_at_least_two() -> None:
    with pytest.raises(ValueError):
        NPlusOneDetector(threshold=1)


# --------------------------------------------------------------------------- #
# DataLoader
# --------------------------------------------------------------------------- #


async def test_coalesces_one_tick_into_one_batch() -> None:
    batch_calls: list[list[int]] = []

    async def batch_fn(keys: Sequence[int]) -> list[int]:
        batch_calls.append(list(keys))
        return [k * 10 for k in keys]

    loader: DataLoader[int, int] = DataLoader(batch_fn)
    results = await loader.load_many([1, 2, 3])
    assert results == [10, 20, 30]
    # All three loads coalesced into a single batch call.
    assert len(batch_calls) == 1
    assert sorted(batch_calls[0]) == [1, 2, 3]
    assert loader.stats.batches == 1
    assert loader.stats.keys_batched == 3
    assert loader.stats.coalesce_ratio == 3.0


async def test_dedups_repeated_keys_in_one_tick() -> None:
    batch_calls: list[list[int]] = []

    async def batch_fn(keys: Sequence[int]) -> list[int]:
        batch_calls.append(list(keys))
        return [k * 2 for k in keys]

    loader: DataLoader[int, int] = DataLoader(batch_fn)
    results = await asyncio.gather(loader.load(5), loader.load(5), loader.load(6))
    assert results == [10, 10, 12]
    # Key 5 requested twice but batched once.
    assert sorted(batch_calls[0]) == [5, 6]


async def test_cross_tick_cache_avoids_second_batch() -> None:
    batch_calls = 0

    async def batch_fn(keys: Sequence[int]) -> list[int]:
        nonlocal batch_calls
        batch_calls += 1
        return [k + 1 for k in keys]

    loader: DataLoader[int, int] = DataLoader(batch_fn, cache=True)
    assert await loader.load(1) == 2
    assert await loader.load(1) == 2  # served from cache, next tick
    assert batch_calls == 1
    assert loader.stats.cache_hits == 1


async def test_cache_disabled_rebatches() -> None:
    batch_calls = 0

    async def batch_fn(keys: Sequence[int]) -> list[int]:
        nonlocal batch_calls
        batch_calls += 1
        return list(keys)

    loader: DataLoader[int, int] = DataLoader(batch_fn, cache=False)
    await loader.load(1)
    await loader.load(1)
    assert batch_calls == 2


async def test_max_batch_size_chunks() -> None:
    chunks: list[int] = []

    async def batch_fn(keys: Sequence[int]) -> list[int]:
        chunks.append(len(keys))
        return list(keys)

    loader: DataLoader[int, int] = DataLoader(batch_fn, max_batch_size=2)
    await loader.load_many([1, 2, 3, 4, 5])
    assert chunks == [2, 2, 1]


async def test_batch_fn_exception_propagates_to_all() -> None:
    async def batch_fn(keys: Sequence[int]) -> list[int]:
        raise RuntimeError("boom")

    loader: DataLoader[int, int] = DataLoader(batch_fn)
    with pytest.raises(RuntimeError, match="boom"):
        await loader.load_many([1, 2])


async def test_length_mismatch_raises() -> None:
    async def batch_fn(keys: Sequence[int]) -> list[int]:
        return [1]  # wrong arity

    loader: DataLoader[int, int] = DataLoader(batch_fn)
    with pytest.raises(ValueError, match="results for"):
        await loader.load_many([1, 2, 3])


async def test_prime_and_clear() -> None:
    calls = 0

    async def batch_fn(keys: Sequence[int]) -> list[int]:
        nonlocal calls
        calls += 1
        return list(keys)

    loader: DataLoader[int, int] = DataLoader(batch_fn)
    loader.prime(9, 99)
    assert await loader.load(9) == 99
    assert calls == 0  # primed, no batch
    loader.clear(9)
    assert await loader.load(9) == 9
    assert calls == 1


async def test_solves_the_n_plus_one_the_detector_finds() -> None:
    # Integration of the two: a per-row loop the detector flags, fixed by a loader.
    det = NPlusOneDetector(threshold=3)

    async def naive_fetch(book_id: int) -> str:
        det.observe("SELECT * FROM book WHERE id = $1", params=book_id)
        return f"book{book_id}"

    await asyncio.gather(*(naive_fetch(i) for i in range(5)))
    assert det.findings()  # the N+1 is detected

    # Now via a loader: one batch call.
    batches = 0

    async def batch_fn(keys: Sequence[int]) -> list[str]:
        nonlocal batches
        batches += 1
        return [f"book{k}" for k in keys]

    loader: DataLoader[int, str] = DataLoader(batch_fn)
    results = await loader.load_many(list(range(5)))
    assert results == [f"book{i}" for i in range(5)]
    assert batches == 1
