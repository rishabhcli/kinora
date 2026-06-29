"""In-process metrics for the acceleration layer.

Plain, dependency-free counters (no prometheus import here — the observability
package can read these snapshots and re-export them). Every accel component owns
one of these and exposes a ``.snapshot()`` so tests can assert on exact savings
without reaching into private state.

Two flavours:

* :class:`SpeculativeMetrics` — proposed/accepted tokens, rounds, target-call
  count, and the derived acceptance rate (the number that drives the adaptive
  draft-length controller).
* :class:`CacheMetrics` — exact-prefix and semantic hits/misses, the derived
  hit-rate, plus stale-eviction and false-positive-guard counts.
* :class:`FanOutMetrics` — races started/won, candidates launched/cancelled,
  cost charged, cap rejections.
* :class:`PrefixReuseMetrics` — prompt tokens served from reused KV vs recomputed.

All counters are guarded by a lock so a multi-task race can update them safely.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass


def _rate(numerator: float, denominator: float) -> float:
    return numerator / denominator if denominator else 0.0


@dataclass(frozen=True, slots=True)
class SpeculativeSnapshot:
    rounds: int
    proposed_tokens: int
    accepted_tokens: int
    bonus_tokens: int
    target_calls: int
    draft_calls: int
    committed_tokens: int

    @property
    def acceptance_rate(self) -> float:
        """Fraction of *proposed* tokens the target accepted."""
        return _rate(self.accepted_tokens, self.proposed_tokens)

    @property
    def tokens_per_target_call(self) -> float:
        """Mean committed tokens per target verification — the speedup proxy."""
        return _rate(self.committed_tokens, self.target_calls)


class SpeculativeMetrics:
    """Thread-safe accumulator of speculative-decoding outcomes."""

    __slots__ = (
        "_accepted",
        "_bonus",
        "_committed",
        "_draft_calls",
        "_lock",
        "_proposed",
        "_rounds",
        "_target_calls",
    )

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._rounds = 0
        self._proposed = 0
        self._accepted = 0
        self._bonus = 0
        self._target_calls = 0
        self._draft_calls = 0
        self._committed = 0

    def record_round(
        self,
        *,
        proposed: int,
        accepted: int,
        bonus: int,
        target_call: bool = True,
        draft_call: bool = True,
    ) -> None:
        with self._lock:
            self._rounds += 1
            self._proposed += proposed
            self._accepted += accepted
            self._bonus += bonus
            self._committed += accepted + bonus
            if target_call:
                self._target_calls += 1
            if draft_call:
                self._draft_calls += 1

    def snapshot(self) -> SpeculativeSnapshot:
        with self._lock:
            return SpeculativeSnapshot(
                rounds=self._rounds,
                proposed_tokens=self._proposed,
                accepted_tokens=self._accepted,
                bonus_tokens=self._bonus,
                target_calls=self._target_calls,
                draft_calls=self._draft_calls,
                committed_tokens=self._committed,
            )


@dataclass(frozen=True, slots=True)
class CacheSnapshot:
    lookups: int
    exact_hits: int
    semantic_hits: int
    misses: int
    stale_evictions: int
    near_miss_rejects: int
    stores: int

    @property
    def hits(self) -> int:
        return self.exact_hits + self.semantic_hits

    @property
    def hit_rate(self) -> float:
        return _rate(self.hits, self.lookups)

    @property
    def semantic_hit_rate(self) -> float:
        return _rate(self.semantic_hits, self.lookups)


class CacheMetrics:
    """Thread-safe accumulator of semantic/exact cache outcomes."""

    __slots__ = (
        "_exact",
        "_lock",
        "_lookups",
        "_misses",
        "_near_miss",
        "_semantic",
        "_stale",
        "_stores",
    )

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._lookups = 0
        self._exact = 0
        self._semantic = 0
        self._misses = 0
        self._stale = 0
        self._near_miss = 0
        self._stores = 0

    def record_lookup(
        self,
        *,
        exact_hit: bool = False,
        semantic_hit: bool = False,
        near_miss: bool = False,
    ) -> None:
        with self._lock:
            self._lookups += 1
            if exact_hit:
                self._exact += 1
            elif semantic_hit:
                self._semantic += 1
            else:
                self._misses += 1
            if near_miss:
                self._near_miss += 1

    def record_store(self) -> None:
        with self._lock:
            self._stores += 1

    def record_stale_eviction(self, count: int = 1) -> None:
        with self._lock:
            self._stale += count

    def snapshot(self) -> CacheSnapshot:
        with self._lock:
            return CacheSnapshot(
                lookups=self._lookups,
                exact_hits=self._exact,
                semantic_hits=self._semantic,
                misses=self._misses,
                stale_evictions=self._stale,
                near_miss_rejects=self._near_miss,
                stores=self._stores,
            )


@dataclass(frozen=True, slots=True)
class FanOutSnapshot:
    races: int
    candidates_started: int
    candidates_cancelled: int
    wins: int
    failures: int
    cap_rejections: int
    cost_charged: float

    @property
    def mean_candidates_per_race(self) -> float:
        return _rate(self.candidates_started, self.races)


class FanOutMetrics:
    """Thread-safe accumulator of fan-out racing outcomes."""

    __slots__ = (
        "_cancelled",
        "_cap_rejections",
        "_cost",
        "_failures",
        "_lock",
        "_races",
        "_started",
        "_wins",
    )

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._races = 0
        self._started = 0
        self._cancelled = 0
        self._wins = 0
        self._failures = 0
        self._cap_rejections = 0
        self._cost = 0.0

    def record_race(
        self,
        *,
        started: int,
        cancelled: int,
        won: bool,
        failures: int,
        cost: float,
    ) -> None:
        with self._lock:
            self._races += 1
            self._started += started
            self._cancelled += cancelled
            self._failures += failures
            self._cost += cost
            if won:
                self._wins += 1

    def record_cap_rejection(self) -> None:
        with self._lock:
            self._cap_rejections += 1

    def snapshot(self) -> FanOutSnapshot:
        with self._lock:
            return FanOutSnapshot(
                races=self._races,
                candidates_started=self._started,
                candidates_cancelled=self._cancelled,
                wins=self._wins,
                failures=self._failures,
                cap_rejections=self._cap_rejections,
                cost_charged=self._cost,
            )


@dataclass(frozen=True, slots=True)
class PrefixReuseSnapshot:
    requests: int
    prompt_tokens_total: int
    prompt_tokens_reused: int
    blocks_reused: int
    blocks_allocated: int

    @property
    def reuse_rate(self) -> float:
        """Fraction of prompt tokens served from a reused prefix's KV cache."""
        return _rate(self.prompt_tokens_reused, self.prompt_tokens_total)


class PrefixReuseMetrics:
    """Thread-safe accumulator of prefix / KV reuse bookkeeping."""

    __slots__ = (
        "_alloc",
        "_lock",
        "_reused_blocks",
        "_reused_tokens",
        "_requests",
        "_total_tokens",
    )

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._requests = 0
        self._total_tokens = 0
        self._reused_tokens = 0
        self._reused_blocks = 0
        self._alloc = 0

    def record_request(
        self,
        *,
        prompt_tokens: int,
        reused_tokens: int,
        blocks_reused: int,
        blocks_allocated: int,
    ) -> None:
        with self._lock:
            self._requests += 1
            self._total_tokens += prompt_tokens
            self._reused_tokens += reused_tokens
            self._reused_blocks += blocks_reused
            self._alloc += blocks_allocated

    def snapshot(self) -> PrefixReuseSnapshot:
        with self._lock:
            return PrefixReuseSnapshot(
                requests=self._requests,
                prompt_tokens_total=self._total_tokens,
                prompt_tokens_reused=self._reused_tokens,
                blocks_reused=self._reused_blocks,
                blocks_allocated=self._alloc,
            )


__all__ = [
    "CacheMetrics",
    "CacheSnapshot",
    "FanOutMetrics",
    "FanOutSnapshot",
    "PrefixReuseMetrics",
    "PrefixReuseSnapshot",
    "SpeculativeMetrics",
    "SpeculativeSnapshot",
]
