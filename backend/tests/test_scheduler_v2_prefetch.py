"""Cold-zone prefetch + eviction policy tests (§4.4/§4.8/§9.7).

Pure, no infra. Pins :func:`app.scheduler.v2.prefetch.plan_cold_zone`: it
pre-warms the nearest forward beats up to the depth, never re-prefetches a cached
key, evicts only when over capacity, never evicts pinned or freshly-prefetched
entries, prefers to evict the furthest-from-playhead material, and keeps a just-
read page over an equidistant far-ahead one (the §4.8 re-read affordance).
"""

from __future__ import annotations

from app.scheduler.v2.prefetch import (
    CacheEntry,
    PrefetchCandidate,
    PrefetchConfig,
    keep_value,
    plan_cold_zone,
)


def _cands(starts: list[int]) -> list[PrefetchCandidate]:
    return [PrefetchCandidate(key=f"k{w}", word_index_start=w) for w in starts]


def _cache(starts: list[int], *, pinned: set[int] | None = None) -> list[CacheEntry]:
    pinned = pinned or set()
    return [
        CacheEntry(key=f"c{w}", word_index_start=w, inserted_seq=i, pinned=w in pinned)
        for i, w in enumerate(starts)
    ]


# --- prefetch ---------------------------------------------------------------- #


def test_prefetches_nearest_forward_up_to_depth() -> None:
    cfg = PrefetchConfig(prefetch_depth=2, cache_capacity=100)
    plan = plan_cold_zone(
        [],
        _cands([500, 100, 300, 700]),
        focus_word=0,
        velocity_wps=4.0,
        config=cfg,
    )
    # Nearest two forward beats, nearest first.
    assert plan.prefetch == ["k100", "k300"]


def test_does_not_prefetch_backward_beats() -> None:
    cfg = PrefetchConfig(prefetch_depth=4, cache_capacity=100)
    plan = plan_cold_zone(
        [],
        _cands([10, 20, 200, 300]),
        focus_word=100,
        velocity_wps=4.0,
        config=cfg,
    )
    # Beats at 10/20 are behind the playhead → not prefetched.
    assert plan.prefetch == ["k200", "k300"]


def test_does_not_reprefetch_cached_key() -> None:
    cfg = PrefetchConfig(prefetch_depth=4, cache_capacity=100)
    cache = [CacheEntry(key="k100", word_index_start=100, inserted_seq=0)]
    plan = plan_cold_zone(
        cache, _cands([100, 200]), focus_word=0, velocity_wps=4.0, config=cfg
    )
    assert "k100" not in plan.prefetch  # already warm
    assert plan.prefetch == ["k200"]


# --- eviction ---------------------------------------------------------------- #


def test_no_eviction_under_capacity() -> None:
    cfg = PrefetchConfig(prefetch_depth=0, cache_capacity=10)
    plan = plan_cold_zone(_cache([100, 200, 300]), [], focus_word=0, velocity_wps=4.0, config=cfg)
    assert plan.evict == []


def test_evicts_when_over_capacity() -> None:
    cfg = PrefetchConfig(prefetch_depth=0, cache_capacity=2)
    # 3 entries, cap 2 → evict 1 (the lowest keep-value = furthest from playhead).
    cache = _cache([100, 5000, 200])
    plan = plan_cold_zone(cache, [], focus_word=0, velocity_wps=4.0, config=cfg)
    assert len(plan.evict) == 1
    assert plan.evict == ["c5000"]  # furthest ahead → lowest keep-value


def test_prefetch_counts_toward_capacity() -> None:
    cfg = PrefetchConfig(prefetch_depth=2, cache_capacity=3)
    # 2 cached + 2 prefetched = 4 > cap 3 → evict 1 existing.
    cache = _cache([100, 9000])
    plan = plan_cold_zone(
        cache, _cands([200, 300]), focus_word=0, velocity_wps=4.0, config=cfg
    )
    assert plan.prefetch == ["k200", "k300"]
    assert plan.evict == ["c9000"]  # the furthest existing entry


def test_pinned_entry_is_never_evicted() -> None:
    cfg = PrefetchConfig(prefetch_depth=0, cache_capacity=1)
    # Two entries, cap 1, but the far one is pinned (in-flight) → evict the near one.
    cache = _cache([100, 9000], pinned={9000})
    plan = plan_cold_zone(cache, [], focus_word=0, velocity_wps=4.0, config=cfg)
    assert "c9000" not in plan.evict
    assert plan.evict == ["c100"]


def test_freshly_prefetched_key_not_evicted_same_pass() -> None:
    cfg = PrefetchConfig(prefetch_depth=1, cache_capacity=1)
    # 1 cached + 1 prefetched = 2 > cap 1 → evict the existing, keep the prefetch.
    cache = _cache([100])
    plan = plan_cold_zone(cache, _cands([200]), focus_word=0, velocity_wps=4.0, config=cfg)
    assert plan.prefetch == ["k200"]
    assert plan.evict == ["c100"]


# --- keep-value / re-read affordance ----------------------------------------- #


def test_just_read_page_outranks_equidistant_far_ahead() -> None:
    cfg = PrefetchConfig(prefetch_depth=0, cache_capacity=1, reread_window_s=60.0)
    # behind: 100 words behind at 4 wps = 25s behind (inside the 60s window).
    # ahead: a page the same |eta| ahead. The behind one should be kept (re-read).
    focus = 400
    v = 4.0
    behind = CacheEntry(key="behind", word_index_start=focus - 100, inserted_seq=0)
    ahead = CacheEntry(key="ahead", word_index_start=focus + 100, inserted_seq=1)
    plan = plan_cold_zone([behind, ahead], [], focus_word=focus, velocity_wps=v, config=cfg)
    # cap 1 → evict the lower keep-value; the behind (re-readable) entry survives.
    assert plan.evict == ["ahead"]


def test_keep_value_is_infinite_for_pinned() -> None:
    cfg = PrefetchConfig()
    pinned = CacheEntry(key="p", word_index_start=99999, inserted_seq=0, pinned=True)
    assert keep_value(pinned, focus_word=0, velocity_wps=4.0, config=cfg) == float("inf")


def test_keep_value_decays_with_distance() -> None:
    cfg = PrefetchConfig()
    near = CacheEntry(key="n", word_index_start=40, inserted_seq=0)
    far = CacheEntry(key="f", word_index_start=4000, inserted_seq=1)
    kn = keep_value(near, focus_word=0, velocity_wps=4.0, config=cfg)
    kf = keep_value(far, focus_word=0, velocity_wps=4.0, config=cfg)
    assert kn > kf  # nearer the playhead = more worth keeping


# --- determinism -------------------------------------------------------------- #


def test_plan_is_deterministic() -> None:
    cfg = PrefetchConfig(prefetch_depth=2, cache_capacity=3)
    cache = _cache([100, 200, 9000])
    cands = _cands([300, 400])
    a = plan_cold_zone(cache, cands, focus_word=0, velocity_wps=4.0, config=cfg)
    b = plan_cold_zone(cache, cands, focus_word=0, velocity_wps=4.0, config=cfg)
    assert a.prefetch == b.prefetch
    assert a.evict == b.evict
