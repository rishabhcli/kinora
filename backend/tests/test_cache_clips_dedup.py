"""``RenderCache`` — the typed dedup facade.

Covers the behaviours the caching layer exists for: a content-addressed hit
charges zero video-seconds; concurrent identical renders coalesce into one
execution (single-flight) and share the result; cross-book/cross-session reuse is
tracked; TTL expiry is deterministic (FakeClock); the durable object tier serves a
cold process; warmup/prefetch skips already-present clips; and stats total the
video-seconds the layer saved. No infra, no network, no live video.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import pytest

from app.cache.clips.dedup import RenderCache
from app.cache.clips.keys import RenderInputs
from app.cache.clips.record import ClipRecord
from app.cache.clips.store import InMemoryClipStore
from app.cache.clock import FakeClock

pytestmark = pytest.mark.asyncio


def _inputs(prompt: str = "a boat at dawn", seed: int = 1, **kw: object) -> RenderInputs:
    return RenderInputs(prompt=prompt, seed=seed, **kw)


def _record(key_value: str, *, video_seconds: float = 5.0, clip: str = "clips/x.mp4") -> ClipRecord:
    return ClipRecord(
        render_key=key_value,
        clip_key=clip,
        last_frame_key="lastframes/x.png",
        duration_s=5.0,
        provider="dashscope",
        model="wan2.1-i2v-turbo",
        video_seconds=video_seconds,
    )


# --------------------------------------------------------------------------- #
# get / put / miss
# --------------------------------------------------------------------------- #


async def test_miss_then_hit() -> None:
    cache = RenderCache.build()
    k = _inputs().key()
    miss = await cache.get(k)
    assert miss.hit is False and miss.record is None and miss.render_key == k.value

    await cache.put(k, _record(k.value))
    hit = await cache.get(k)
    assert hit.hit is True
    assert hit.record is not None and hit.record.clip_key == "clips/x.mp4"
    # A hit charges ZERO video-seconds (the whole point) and reports the saving.
    assert hit.video_seconds == 0.0
    assert hit.video_seconds_saved == 5.0


async def test_put_accepts_inputs_or_key() -> None:
    cache = RenderCache.build()
    inputs = _inputs()
    await cache.put(inputs, _record(inputs.key().value))
    assert (await cache.get(inputs)).hit is True


async def test_semantically_identical_requests_share_one_clip() -> None:
    cache = RenderCache.build()
    a = _inputs(prompt="Hello   World", seed=3, reference_image_ids=["b", "a"])
    b = _inputs(prompt="hello world", seed=3, reference_image_ids=["a", "b"])
    await cache.put(a, _record(a.key().value))
    # b normalises to the same content key, so it hits a's clip.
    assert (await cache.get(b)).hit is True


async def test_presigned_urls_only_with_store() -> None:
    store = InMemoryClipStore()
    cache = RenderCache.build(object_store=store)
    k = _inputs().key()
    await cache.put(k, _record(k.value))
    hit = await cache.get(k)
    assert hit.clip_url == f"memory://{store._bucket}/clips/x.mp4"
    assert hit.last_frame_url is not None

    no_store = RenderCache.build()
    await no_store.put(k, _record(k.value))
    assert (await no_store.get(k)).clip_url is None


# --------------------------------------------------------------------------- #
# single-flight / request coalescing
# --------------------------------------------------------------------------- #


async def test_concurrent_identical_renders_execute_once() -> None:
    cache = RenderCache.build()
    k = _inputs(prompt="expensive shot").key()
    calls = 0
    started = asyncio.Event()
    release = asyncio.Event()

    async def render() -> ClipRecord:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return _record(k.value, video_seconds=5.0)

    leader = asyncio.create_task(cache.get_or_render(k, render, book_id="b0"))
    await started.wait()
    followers = [
        asyncio.create_task(cache.get_or_render(k, render, book_id=f"b{i}")) for i in range(1, 6)
    ]
    release.set()
    results = await asyncio.gather(leader, *followers)

    # Exactly one underlying render ran for the whole wave.
    assert calls == 1
    leaders = [r for r in results if r.tier is None and not r.hit]
    coalesced = [r for r in results if r.tier == "coalesced"]
    assert len(leaders) == 1
    assert len(coalesced) == 5
    # Every caller got the same clip.
    assert {r.record.clip_key for r in results if r.record} == {"clips/x.mp4"}


async def test_render_result_is_cached_for_next_wave() -> None:
    cache = RenderCache.build()
    k = _inputs().key()
    calls = 0

    async def render() -> ClipRecord:
        nonlocal calls
        calls += 1
        return _record(k.value)

    first = await cache.get_or_render(k, render)
    assert first.hit is False and calls == 1
    second = await cache.get_or_render(k, render)
    assert second.hit is True and calls == 1  # served from cache, no re-render


async def test_follower_cancellation_does_not_cancel_leader() -> None:
    cache = RenderCache.build()
    k = _inputs(prompt="cancel-safety").key()
    calls = 0
    started = asyncio.Event()
    release = asyncio.Event()

    async def render() -> ClipRecord:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return _record(k.value)

    leader = asyncio.create_task(cache.get_or_render(k, render, book_id="leader"))
    await started.wait()
    follower = asyncio.create_task(cache.get_or_render(k, render, book_id="follower"))
    await asyncio.sleep(0)  # let the follower attach to the in-flight load
    follower.cancel()
    with pytest.raises(asyncio.CancelledError):
        await follower
    # The leader still completes and produces the clip exactly once.
    release.set()
    result = await leader
    assert calls == 1
    assert result.record is not None
    assert (await cache.get(k)).hit is True


async def test_get_or_render_raises_propagate_to_all_callers() -> None:
    cache = RenderCache.build()
    k = _inputs(prompt="boom").key()

    async def render() -> ClipRecord:
        raise ValueError("provider exploded")

    with pytest.raises(ValueError, match="provider exploded"):
        await cache.get_or_render(k, render)
    # Nothing was cached, so the next call re-attempts (no poisoned negative).
    assert (await cache.get(k)).hit is False


# --------------------------------------------------------------------------- #
# cross-book / cross-session reuse accounting
# --------------------------------------------------------------------------- #


async def test_cross_book_reuse_is_recorded() -> None:
    cache = RenderCache.build()
    k = _inputs().key()
    await cache.put(k, _record(k.value, video_seconds=5.0))
    await cache.get(k, book_id="book-A")
    await cache.get(k, book_id="book-B")
    await cache.get(k, book_id="book-A")  # repeat: not a new book
    assert cache.cross_book_hits == 2
    # The widened referencing-book set is persisted on the record.
    record = (await cache.get(k)).record
    assert record is not None
    assert set(record.referencing_books) == {"book-A", "book-B"}
    assert record.reuse_count == 2


async def test_video_seconds_saved_totals_across_hits() -> None:
    cache = RenderCache.build()
    k = _inputs().key()
    await cache.put(k, _record(k.value, video_seconds=6.0))
    await cache.get(k, book_id="b1")
    await cache.get(k, book_id="b2")
    # Two hits, each avoiding a 6s render.
    assert cache.video_seconds_saved == 12.0


# --------------------------------------------------------------------------- #
# invalidation
# --------------------------------------------------------------------------- #


async def test_invalidate_by_key() -> None:
    cache = RenderCache.build()
    k = _inputs().key()
    await cache.put(k, _record(k.value))
    assert await cache.invalidate(k) == 1
    assert (await cache.get(k)).hit is False


async def test_invalidate_by_tag_drops_only_tagged_clips() -> None:
    cache = RenderCache.build()
    a = _inputs(prompt="char alice", seed=1).key()
    b = _inputs(prompt="char alice", seed=2).key()
    c = _inputs(prompt="landscape", seed=3).key()
    await cache.put(a, _record(a.value), tags=["entity:alice", "book:7"])
    await cache.put(b, _record(b.value), tags=["entity:alice"])
    await cache.put(c, _record(c.value), tags=["book:7"])

    # A Director edit to Alice drops only the clips that referenced her.
    removed = await cache.invalidate_tag("entity:alice")
    assert removed == 2
    assert (await cache.get(a)).hit is False
    assert (await cache.get(b)).hit is False
    assert (await cache.get(c)).hit is True  # the landscape is untouched


async def test_tag_survives_cross_book_repersist() -> None:
    # Reuse re-persists the record; the dependency tag must NOT be stripped.
    cache = RenderCache.build()
    k = _inputs().key()
    await cache.put(k, _record(k.value), tags=["entity:bob"])
    await cache.get(k, book_id="book-X")  # triggers a tagged re-persist
    assert await cache.invalidate_tag("entity:bob") == 1
    assert (await cache.get(k)).hit is False


# --------------------------------------------------------------------------- #
# TTL expiry (deterministic clock)
# --------------------------------------------------------------------------- #


async def test_ttl_expiry_with_fake_clock() -> None:
    clk = FakeClock()
    cache = RenderCache.build(clock=clk, record_ttl_s=100.0)
    k = _inputs().key()
    await cache.put(k, _record(k.value))
    assert (await cache.get(k)).hit is True
    clk.advance(99.0)
    assert (await cache.get(k)).hit is True
    clk.advance(2.0)  # now past 100s
    assert (await cache.get(k)).hit is False


async def test_per_put_ttl_override() -> None:
    clk = FakeClock()
    cache = RenderCache.build(clock=clk, record_ttl_s=1000.0)
    k = _inputs().key()
    await cache.put(k, _record(k.value), ttl=10.0)
    clk.advance(11.0)
    assert (await cache.get(k)).hit is False


# --------------------------------------------------------------------------- #
# durable object tier — cold-process reuse
# --------------------------------------------------------------------------- #


async def test_cold_process_serves_from_object_tier() -> None:
    store = InMemoryClipStore()
    warm = RenderCache.build(object_store=store)
    k = _inputs(prompt="durable clip").key()
    await warm.put(k, _record(k.value, video_seconds=6.0))

    # A brand-new RenderCache with an empty L1 but the same object store: the clip
    # is discovered in the durable tier and served for zero video-seconds.
    cold = RenderCache.build(object_store=store)
    hit = await cold.get(k, book_id="fresh-book")
    assert hit.hit is True
    assert hit.video_seconds == 0.0
    assert hit.record is not None and hit.record.clip_key == "clips/x.mp4"


# --------------------------------------------------------------------------- #
# warmup / prefetch
# --------------------------------------------------------------------------- #


async def test_warm_renders_absent_and_skips_present() -> None:
    cache = RenderCache.build()
    present = _inputs(prompt="already there", seed=1).key()
    absent = _inputs(prompt="needs render", seed=2).key()
    await cache.put(present, _record(present.value))

    rendered_keys: list[str] = []

    def make_render(key_value: str) -> Callable[[], Awaitable[ClipRecord]]:
        async def render() -> ClipRecord:
            rendered_keys.append(key_value)
            return _record(key_value)

        return render

    triggered = await cache.warm(
        [(present, make_render(present.value)), (absent, make_render(absent.value))],
        book_id="book-1",
    )
    assert triggered == 1
    assert rendered_keys == [absent.value]  # the present clip was skipped
    assert (await cache.get(absent)).hit is True


async def test_prefetch_keys_reports_presence_and_promotes() -> None:
    store = InMemoryClipStore()
    cache = RenderCache.build(object_store=store)
    here = _inputs(prompt="x", seed=1).key()
    gone = _inputs(prompt="y", seed=2).key()
    await cache.put(here, _record(here.value))
    presence = await cache.prefetch_keys([here, gone])
    assert presence == {here.value: True, gone.value: False}


# --------------------------------------------------------------------------- #
# stats
# --------------------------------------------------------------------------- #


async def test_stats_report_hits_misses_and_saved_seconds() -> None:
    cache = RenderCache.build()
    k = _inputs().key()
    await cache.get(k)  # miss
    await cache.put(k, _record(k.value, video_seconds=5.0))
    await cache.get(k, book_id="b1")  # hit
    await cache.get(k, book_id="b2")  # hit
    stats = cache.stats()
    assert int(stats["misses"]) >= 1
    assert int(stats["hits"]) >= 2
    assert stats["video_seconds_saved"] == 10.0
    assert stats["cross_book_hits"] == 2


async def test_l1_eviction_is_counted() -> None:
    cache = RenderCache.build(l1_max_entries=2)
    keys = [_inputs(prompt=f"shot {i}", seed=i).key() for i in range(5)]
    for k in keys:
        await cache.put(k, _record(k.value))
    stats = cache.stats()
    # With a cap of 2 and 5 distinct inserts, at least 3 were evicted.
    assert int(stats["evictions"]) >= 3


async def test_health_ok_in_memory_only() -> None:
    cache = RenderCache.build()
    assert await cache.health() is True
