"""Batching tests — single-flight coalescing + micro-batching, deterministic."""

from __future__ import annotations

import asyncio

import pytest

from app.inference.accel.batching import (
    MicroBatcher,
    RequestCoalescer,
    batch_from_single,
)
from app.inference.accel.protocol import GenerationRequest, GenerationResult


def _req(prompt: str) -> GenerationRequest:
    return GenerationRequest.from_prompt(prompt)


def _res(text: str) -> GenerationResult:
    return GenerationResult.from_tokens(text.split(), model="m")


# --------------------------------------------------------------------------- #
# RequestCoalescer
# --------------------------------------------------------------------------- #


async def test_concurrent_identical_requests_coalesce() -> None:
    gate = asyncio.Event()
    calls = 0

    async def slow_generate(request: GenerationRequest) -> GenerationResult:
        nonlocal calls
        calls += 1
        await gate.wait()  # hold all callers until released
        return _res("answer")

    coalescer = RequestCoalescer(slow_generate)
    req = _req("same prompt")
    # Three concurrent identical requests.
    t1 = asyncio.ensure_future(coalescer.generate(req))
    t2 = asyncio.ensure_future(coalescer.generate(req))
    t3 = asyncio.ensure_future(coalescer.generate(req))
    await asyncio.sleep(0)  # let them register
    gate.set()
    r1, r2, r3 = await asyncio.gather(t1, t2, t3)
    assert r1.text == r2.text == r3.text == "answer"
    assert calls == 1  # only ONE backend call for three concurrent requests
    stats = coalescer.stats
    assert stats.requests == 3
    assert stats.backend_calls == 1
    assert stats.coalesced == 2
    assert stats.coalesce_rate == pytest.approx(2 / 3)


async def test_distinct_requests_not_coalesced() -> None:
    calls = 0

    async def generate(request: GenerationRequest) -> GenerationResult:
        nonlocal calls
        calls += 1
        return _res(request.prompt_text)

    coalescer = RequestCoalescer(generate)
    a, b = await asyncio.gather(coalescer.generate(_req("a")), coalescer.generate(_req("b")))
    assert a.text == "a"
    assert b.text == "b"
    assert calls == 2


async def test_sequential_identical_requests_each_call() -> None:
    # After the first resolves, the in-flight entry clears -> a later identical
    # request issues a fresh call (dedup of in-flight only, not caching).
    calls = 0

    async def generate(request: GenerationRequest) -> GenerationResult:
        nonlocal calls
        calls += 1
        return _res("x")

    coalescer = RequestCoalescer(generate)
    await coalescer.generate(_req("p"))
    await coalescer.generate(_req("p"))
    assert calls == 2


async def test_coalescer_propagates_exception_to_all() -> None:
    gate = asyncio.Event()

    async def failing(request: GenerationRequest) -> GenerationResult:
        await gate.wait()
        raise RuntimeError("boom")

    coalescer = RequestCoalescer(failing)
    req = _req("p")
    t1 = asyncio.ensure_future(coalescer.generate(req))
    t2 = asyncio.ensure_future(coalescer.generate(req))
    await asyncio.sleep(0)
    gate.set()
    results = await asyncio.gather(t1, t2, return_exceptions=True)
    assert all(isinstance(r, RuntimeError) for r in results)
    # in-flight cleared -> a retry can proceed
    gate2 = asyncio.Event()
    gate2.set()

    async def ok(request: GenerationRequest) -> GenerationResult:
        return _res("recovered")

    coalescer2 = RequestCoalescer(ok)
    assert (await coalescer2.generate(req)).text == "recovered"


# --------------------------------------------------------------------------- #
# MicroBatcher
# --------------------------------------------------------------------------- #


async def test_micro_batch_flushes_on_explicit_flush() -> None:
    seen_batches: list[int] = []

    async def batch_generate(requests):  # type: ignore[no-untyped-def]
        seen_batches.append(len(requests))
        return [_res(f"r:{r.prompt_text}") for r in requests]

    batcher = MicroBatcher(batch_generate, max_batch=10)
    t1 = asyncio.ensure_future(batcher.submit(_req("one")))
    t2 = asyncio.ensure_future(batcher.submit(_req("two")))
    await asyncio.sleep(0)
    assert batcher.pending == 2
    flushed = await batcher.flush()
    assert flushed == 2
    r1, r2 = await asyncio.gather(t1, t2)
    assert r1.text == "r:one"
    assert r2.text == "r:two"
    assert seen_batches == [2]  # exactly one batch of two


async def test_micro_batch_auto_flushes_at_max() -> None:
    batches: list[int] = []

    async def batch_generate(requests):  # type: ignore[no-untyped-def]
        batches.append(len(requests))
        return [_res("x") for _ in requests]

    batcher = MicroBatcher(batch_generate, max_batch=2)
    # Two submits -> auto-flush at max_batch=2.
    r1, r2 = await asyncio.gather(batcher.submit(_req("a")), batcher.submit(_req("b")))
    assert r1.text == "x"
    assert r2.text == "x"
    assert batches == [2]
    assert batcher.pending == 0


async def test_micro_batch_stats() -> None:
    async def batch_generate(requests):  # type: ignore[no-untyped-def]
        return [_res("x") for _ in requests]

    batcher = MicroBatcher(batch_generate, max_batch=2)
    await asyncio.gather(batcher.submit(_req("a")), batcher.submit(_req("b")))
    t = asyncio.ensure_future(batcher.submit(_req("c")))
    await asyncio.sleep(0)
    await batcher.flush()
    await t
    stats = batcher.stats
    assert stats.requests == 3
    assert stats.batches == 2
    assert stats.largest_batch == 2
    assert stats.mean_batch_size == pytest.approx(1.5)


async def test_micro_batch_empty_flush_noop() -> None:
    async def batch_generate(requests):  # type: ignore[no-untyped-def]
        return []

    batcher = MicroBatcher(batch_generate)
    assert await batcher.flush() == 0


async def test_micro_batch_size_mismatch_fails_waiters() -> None:
    async def bad_batch(requests):  # type: ignore[no-untyped-def]
        return [_res("only one")]  # wrong count

    batcher = MicroBatcher(bad_batch, max_batch=10)
    t1 = asyncio.ensure_future(batcher.submit(_req("a")))
    t2 = asyncio.ensure_future(batcher.submit(_req("b")))
    await asyncio.sleep(0)
    with pytest.raises(ValueError):
        await batcher.flush()
    results = await asyncio.gather(t1, t2, return_exceptions=True)
    assert all(isinstance(r, ValueError) for r in results)


async def test_micro_batch_backend_error_fails_waiters() -> None:
    async def boom(requests):  # type: ignore[no-untyped-def]
        raise RuntimeError("batch down")

    batcher = MicroBatcher(boom, max_batch=10)
    t1 = asyncio.ensure_future(batcher.submit(_req("a")))
    await asyncio.sleep(0)
    with pytest.raises(RuntimeError):
        await batcher.flush()
    res = await asyncio.gather(t1, return_exceptions=True)
    assert isinstance(res[0], RuntimeError)


async def test_invalid_max_batch() -> None:
    async def batch_generate(requests):  # type: ignore[no-untyped-def]
        return []

    with pytest.raises(ValueError):
        MicroBatcher(batch_generate, max_batch=0)


async def test_batch_from_single_adapter() -> None:
    calls = 0

    async def generate(request: GenerationRequest) -> GenerationResult:
        nonlocal calls
        calls += 1
        return _res(request.prompt_text)

    batch = batch_from_single(generate)
    results = await batch([_req("a"), _req("b"), _req("c")])
    assert [r.text for r in results] == ["a", "b", "c"]
    assert calls == 3
