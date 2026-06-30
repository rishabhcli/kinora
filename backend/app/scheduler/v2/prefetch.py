"""Cold-zone prefetch + eviction policy (kinora.md §4.4/§4.8/§9.7).

The §4.4 zones split upcoming content into committed (full video), speculative
(cheap keyframes, no video-seconds), and **cold** (plan/canon only — nothing
rendered). The cold zone is where the scheduler does the least, by design: pages
the reader is minutes away from don't earn a render. But two cheap, zero-video
optimisations live exactly there:

1. **Prefetch.** A small number of beats *just past* the speculative horizon are
   worth warming a keyframe for *ahead of* their promotion, so that when the
   reader's advance pulls them across the horizon the still is already in hand and
   the keyframe ladder never shows a blank. This is image-gen / canon-reference
   work (§4.4) — it never draws down the video-seconds budget — so the only cost
   to manage is the cheap-lane queue depth and a bounded warm cache.

2. **Eviction.** Backward seeks make re-reads "essentially free" because accepted
   shots replay straight from object storage (§4.8/§9.7). To keep that true the
   warm cache holds recently-passed and near-future material, but it is bounded:
   when it overflows we must evict *something*. The right victim is the entry the
   reader is **least likely to need soon** — far behind (already read, unlikely to
   re-read) or far ahead beyond any plausible near-term arrival — scored by
   reading-time distance from the focus playhead in *either* direction.

This module is a **pure planner** over a snapshot of the warm cache and the
reader's position/velocity. It decides *what to prefetch* and *what to evict*; it
performs no I/O, enqueues nothing, and — because everything it touches is the
keyframe/cache lane, never the committed video lane — it spends **zero**
video-seconds. It composes with the §4.9 keyframe maintainer: the scheduler feeds
its prefetch list into ``ensure_keyframe`` (cheap lane) and drops the evicted ids.

Eviction scoring
----------------
Each cached entry gets a **keep-value** = recency-decayed utility:

* near the playhead (small ``|eta|``) ⇒ high value (about to be needed, or just
  read and re-readable);
* far ahead or far behind ⇒ low value;
* a small *behind* bonus models the §4.8 re-read affordance — a just-passed page
  is cheap to keep and likely re-read, so it outranks an equidistant far-ahead
  page.

The lowest-keep-value entries are evicted until the cache is back under capacity.
Ties break on larger absolute ETA then insertion order, so eviction is fully
deterministic for the simulator.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from app.scheduler.zones import eta_seconds

#: Reading-seconds behind the playhead within which a just-read page is still
#: considered cheaply re-readable (the §4.8 backward-seek affordance window).
DEFAULT_REREAD_WINDOW_S = 60.0
#: Multiplicative keep-bonus for entries *behind* the playhead inside the re-read
#: window (they outrank equidistant far-ahead entries on eviction).
DEFAULT_BEHIND_BONUS = 1.25
#: Default number of beats past the speculative horizon to pre-warm.
DEFAULT_PREFETCH_DEPTH = 4
#: Default warm-cache capacity (entries) before eviction kicks in.
DEFAULT_CACHE_CAPACITY = 24


@dataclass(frozen=True, slots=True)
class CacheEntry:
    """One warm keyframe/clip held in the cold-zone cache (a thin view).

    ``word_index_start`` locates it in the book; ``inserted_seq`` is a monotone
    counter for deterministic tie-breaking; ``pinned`` protects committed/in-flight
    material from eviction (never drop what's actively rendering).
    """

    key: str
    word_index_start: int
    inserted_seq: int = 0
    pinned: bool = False


@dataclass(frozen=True, slots=True)
class PrefetchCandidate:
    """A beat just past the speculative horizon that could be pre-warmed."""

    key: str
    word_index_start: int


@dataclass(slots=True)
class PrefetchPlan:
    """The cold-zone plan: keys to pre-warm now + keys to evict (§4.4/§9.7).

    ``prefetch`` is ordered nearest-first (warm the soonest-needed beat first);
    ``evict`` is ordered lowest-keep-value first. Both are pure suggestions — the
    caller routes ``prefetch`` to the cheap keyframe lane and drops ``evict`` from
    its cache. No video-seconds anywhere.
    """

    prefetch: list[str] = field(default_factory=list)
    evict: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class PrefetchConfig:
    """Tunable knobs for the cold-zone policy (default to the constants)."""

    prefetch_depth: int = DEFAULT_PREFETCH_DEPTH
    cache_capacity: int = DEFAULT_CACHE_CAPACITY
    reread_window_s: float = DEFAULT_REREAD_WINDOW_S
    behind_bonus: float = DEFAULT_BEHIND_BONUS


def keep_value(
    entry: CacheEntry,
    *,
    focus_word: int,
    velocity_wps: float,
    config: PrefetchConfig,
) -> float:
    """The eviction keep-value of ``entry`` — higher = more worth keeping (§4.8).

    A pinned entry is effectively infinite (never evicted). Otherwise the value
    decays with absolute reading-time distance from the playhead, with a bonus for
    entries just *behind* the playhead inside the re-read window (cheap to keep,
    likely re-read per §4.8). Pure and monotone, so eviction order is stable.
    """
    if entry.pinned:
        return math.inf
    eta = eta_seconds(entry.word_index_start, focus_word, velocity_wps)
    # exp decay in |eta| over the re-read window scale.
    scale = max(1.0, config.reread_window_s)
    value = math.exp(-abs(eta) / scale)
    # Behind the playhead and inside the window ⇒ re-read bonus.
    if -config.reread_window_s <= eta < 0.0:
        value *= config.behind_bonus
    return value


def plan_cold_zone(
    cache: list[CacheEntry],
    prefetch_candidates: list[PrefetchCandidate],
    *,
    focus_word: int,
    velocity_wps: float,
    config: PrefetchConfig | None = None,
) -> PrefetchPlan:
    """Decide what to pre-warm and what to evict from the cold-zone cache (§4.4).

    Prefetch: take the ``prefetch_depth`` nearest *forward* candidates not already
    cached, ordered nearest-first — the beats the reader's advance will pull across
    the speculative horizon soonest.

    Eviction: if the cache (plus the about-to-be-added prefetch keys) would exceed
    ``cache_capacity``, evict the lowest-keep-value *unpinned* entries until back
    under capacity. Newly-prefetched keys are never evicted in the same pass (they
    were just chosen as worth warming).

    Pure and deterministic; zero video-seconds (cold zone is keyframe/canon only).
    """
    cfg = config or PrefetchConfig()
    cached_keys = {e.key for e in cache}

    # --- prefetch: nearest forward, not-yet-cached, up to depth ----------- #
    forward = [
        c
        for c in prefetch_candidates
        if c.word_index_start > focus_word and c.key not in cached_keys
    ]
    forward.sort(key=lambda c: c.word_index_start)
    prefetch = [c.key for c in forward[: max(0, cfg.prefetch_depth)]]
    prefetch_set = set(prefetch)

    # --- eviction: trim to capacity by lowest keep-value ------------------ #
    # Project the post-prefetch cache size: existing entries + newly warmed keys.
    projected_size = len(cache) + len(prefetch)
    overflow = projected_size - cfg.cache_capacity
    evict: list[str] = []
    if overflow > 0:
        evictable = [e for e in cache if not e.pinned and e.key not in prefetch_set]
        # Sort by (keep-value asc, |eta| desc, insertion seq asc) — deterministic.
        scored = sorted(
            evictable,
            key=lambda e: (
                keep_value(
                    e, focus_word=focus_word, velocity_wps=velocity_wps, config=cfg
                ),
                -abs(eta_seconds(e.word_index_start, focus_word, velocity_wps)),
                e.inserted_seq,
            ),
        )
        evict = [e.key for e in scored[:overflow]]

    return PrefetchPlan(prefetch=prefetch, evict=evict)


__all__ = [
    "DEFAULT_BEHIND_BONUS",
    "DEFAULT_CACHE_CAPACITY",
    "DEFAULT_PREFETCH_DEPTH",
    "DEFAULT_REREAD_WINDOW_S",
    "CacheEntry",
    "PrefetchCandidate",
    "PrefetchConfig",
    "PrefetchPlan",
    "keep_value",
    "plan_cold_zone",
]
