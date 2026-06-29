"""Semantic-cache tests — exact + semantic hits, thresholds, staleness, eviction."""

from __future__ import annotations

import pytest

from app.inference.accel.clock import FakeClock
from app.inference.accel.fakes import HashEmbedder
from app.inference.accel.metrics import CacheMetrics
from app.inference.accel.protocol import GenerationRequest, GenerationResult
from app.inference.accel.semantic_cache import (
    CacheConfig,
    SemanticCache,
    cosine,
    exact_key,
)


def _req(prompt: str, *, temperature: float = 0.0, model: str = "m") -> GenerationRequest:
    return GenerationRequest.from_prompt(prompt, model=model, temperature=temperature)


def _res(text: str) -> GenerationResult:
    return GenerationResult.from_tokens(text.split(), model="m")


def _cache(
    *, alias: dict[str, str] | None = None, config: CacheConfig | None = None, clock: FakeClock
) -> SemanticCache:
    return SemanticCache(
        HashEmbedder(dim=32, alias=alias),
        config=config,
        metrics=CacheMetrics(),
        clock=clock,
    )


# --------------------------------------------------------------------------- #
# Primitives
# --------------------------------------------------------------------------- #


def test_cosine_basics() -> None:
    assert cosine((1.0, 0.0), (1.0, 0.0)) == pytest.approx(1.0)
    assert cosine((1.0, 0.0), (0.0, 1.0)) == pytest.approx(0.0)
    assert cosine((0.0, 0.0), (1.0, 0.0)) == 0.0
    with pytest.raises(ValueError):
        cosine((1.0,), (1.0, 2.0))


def test_exact_key_stable_and_param_sensitive() -> None:
    a = exact_key(_req("hello"))
    b = exact_key(_req("hello"))
    assert a == b
    # different temperature -> different key
    assert exact_key(_req("hello", temperature=0.0)) != exact_key(_req("hello", temperature=0.2))


# --------------------------------------------------------------------------- #
# Exact + semantic hits
# --------------------------------------------------------------------------- #


async def test_exact_hit() -> None:
    cache = _cache(clock=FakeClock())
    req = _req("the cat sat")
    assert (await cache.lookup(req)).kind == "miss"
    await cache.store(req, _res("a feline rested"))
    out = await cache.lookup(req)
    assert out.kind == "exact"
    assert out.result is not None
    assert out.result.text == "a feline rested"
    assert out.result.meta["cache"] == "exact"


async def test_semantic_hit_via_alias() -> None:
    # Two different prompt strings forced to the same embedding (perfect dup).
    cache = _cache(alias={"please summarize chapter one": "summarize chapter 1"}, clock=FakeClock())
    stored = _req("summarize chapter 1")
    await cache.store(stored, _res("it begins"))
    # A *different* exact key, identical embedding -> semantic hit.
    query = _req("please summarize chapter one")
    assert exact_key(query) != exact_key(stored)
    out = await cache.lookup(query)
    assert out.kind == "semantic"
    assert out.result is not None
    assert out.result.text == "it begins"
    assert out.result.meta["cache"] == "semantic"
    assert out.similarity == pytest.approx(1.0)


async def test_below_threshold_is_miss_and_near_miss_tracked() -> None:
    # No alias -> distinct prompts embed to (almost surely) low similarity.
    cfg = CacheConfig(similarity_threshold=0.99, near_miss_margin=2.0)
    cache = _cache(config=cfg, clock=FakeClock())
    await cache.store(_req("alpha beta gamma"), _res("x"))
    out = await cache.lookup(_req("totally unrelated words here"))
    assert out.kind == "miss"
    assert out.result is None
    snap = cache.metrics.snapshot()
    # near_miss_margin huge -> the miss counts as a near-miss for telemetry.
    assert snap.near_miss_rejects == 1


# --------------------------------------------------------------------------- #
# Versioning / staleness
# --------------------------------------------------------------------------- #


async def test_version_bump_invalidates() -> None:
    cache = _cache(clock=FakeClock())
    req = _req("canon fact")
    await cache.store(req, _res("v1 answer"), namespace="book")
    assert (await cache.lookup(req, namespace="book")).kind == "exact"
    cache.bump_version("book")  # canon changed
    out = await cache.lookup(req, namespace="book")
    assert out.kind == "miss"  # old entry is stale
    # storing again under the new version works
    await cache.store(req, _res("v2 answer"), namespace="book")
    out2 = await cache.lookup(req, namespace="book")
    assert out2.result is not None
    assert out2.result.text == "v2 answer"


async def test_ttl_expiry() -> None:
    clock = FakeClock()
    cfg = CacheConfig(ttl_s=10.0)
    cache = _cache(config=cfg, clock=clock)
    req = _req("ephemeral")
    await cache.store(req, _res("soon gone"))
    assert (await cache.lookup(req)).kind == "exact"
    clock.advance(11.0)
    assert (await cache.lookup(req)).kind == "miss"
    assert cache.metrics.snapshot().stale_evictions >= 1


async def test_namespace_isolation() -> None:
    cache = _cache(clock=FakeClock())
    req = _req("shared prompt")
    await cache.store(req, _res("for A"), namespace="A")
    assert (await cache.lookup(req, namespace="B")).kind == "miss"
    assert (await cache.lookup(req, namespace="A")).kind == "exact"


async def test_invalidate_namespace_drops_entries() -> None:
    cache = _cache(clock=FakeClock())
    await cache.store(_req("one"), _res("1"), namespace="ns")
    await cache.store(_req("two"), _res("2"), namespace="ns")
    dropped = cache.invalidate_namespace("ns")
    assert dropped == 2
    assert cache.size() == 0


# --------------------------------------------------------------------------- #
# Eviction + temperature gate
# --------------------------------------------------------------------------- #


async def test_lru_eviction_bounds_size() -> None:
    cfg = CacheConfig(max_entries=2)
    cache = _cache(config=cfg, clock=FakeClock())
    await cache.store(_req("a"), _res("1"))
    await cache.store(_req("b"), _res("2"))
    await cache.lookup(_req("a"))  # touch 'a' -> 'b' is now LRU
    await cache.store(_req("c"), _res("3"))  # evicts 'b'
    assert cache.size() == 2
    assert (await cache.lookup(_req("a"))).kind == "exact"
    assert (await cache.lookup(_req("b"))).kind == "miss"
    assert (await cache.lookup(_req("c"))).kind == "exact"


async def test_high_temperature_not_cached() -> None:
    cfg = CacheConfig(max_cacheable_temperature=0.3)
    cache = _cache(config=cfg, clock=FakeClock())
    req = _req("hot", temperature=0.9)
    stored = await cache.store(req, _res("sampled"))
    assert stored is False
    assert cache.size() == 0


# --------------------------------------------------------------------------- #
# get_or_compute + threshold tuning
# --------------------------------------------------------------------------- #


async def test_get_or_compute_calls_once() -> None:
    cache = _cache(clock=FakeClock())
    calls = 0

    async def compute(_r: GenerationRequest) -> GenerationResult:
        nonlocal calls
        calls += 1
        return _res("computed")

    req = _req("expensive prompt")
    r1 = await cache.get_or_compute(req, compute)
    r2 = await cache.get_or_compute(req, compute)
    assert calls == 1  # second call served from cache
    assert r1.meta["cache"] == "miss"
    assert r2.meta["cache"] == "exact"


async def test_set_threshold_runtime() -> None:
    cache = _cache(
        alias={"q2": "q1"}, config=CacheConfig(similarity_threshold=1.01), clock=FakeClock()
    )
    await cache.store(_req("q1"), _res("ans"))
    # threshold above 1.0 -> even a perfect dup misses
    assert (await cache.lookup(_req("q2"))).kind == "miss"
    cache.set_threshold(0.99)
    assert (await cache.lookup(_req("q2"))).kind == "semantic"


async def test_hit_rate_metric() -> None:
    cache = _cache(clock=FakeClock())
    req = _req("p")
    await cache.lookup(req)  # miss
    await cache.store(req, _res("v"))
    await cache.lookup(req)  # exact hit
    await cache.lookup(req)  # exact hit
    snap = cache.metrics.snapshot()
    assert snap.lookups == 3
    assert snap.hits == 2
    assert snap.hit_rate == pytest.approx(2 / 3)
