"""Router metrics + a percentile-capable latency/wait sketch (§12.5).

The router emits, in the spirit of §12.5 ("emit per request: latency, retries,
cache hit/miss, ... per session: buffer occupancy, ..."), a compact set of
counters + a streaming quantile sketch so the scaling facet's SLO controller and
the demo metrics panel can read queue-wait + service-time percentiles without
retaining every sample.

:class:`RouterStats` is a plain mutable accumulator (cheap to snapshot); the
:class:`P2Quantile` estimator is the classic P² algorithm — O(1) memory per
tracked quantile, no sample buffer, deterministic. It is what lets us assert a
queue-wait-p99 SLO in the simulator without storing the full trace.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .admission import RejectReason
from .request import RequestPriority


class P2Quantile:
    """Single-quantile P² estimator (Jain & Chlamtac) — O(1) memory, streaming.

    Tracks an online estimate of one quantile ``p`` (e.g. 0.99). Exact for the
    first 5 observations, then updates 5 markers per sample. Deterministic.
    """

    def __init__(self, p: float) -> None:
        if not 0.0 < p < 1.0:
            raise ValueError("p must be in (0, 1)")
        self.p = p
        self._n: list[int] = []
        self._q: list[float] = []
        self._np = [0.0] * 5
        self._dn = [0.0, p / 2, p, (1 + p) / 2, 1.0]
        self._count = 0

    def observe(self, x: float) -> None:
        self._count += 1
        if len(self._q) < 5:
            self._q.append(x)
            self._q.sort()
            if len(self._q) == 5:
                self._n = [1, 2, 3, 4, 5]
                self._np = [1, 1 + 2 * self.p, 1 + 4 * self.p, 3 + 2 * self.p, 5]
            return
        # Find cell k.
        if x < self._q[0]:
            self._q[0] = x
            k = 0
        elif x >= self._q[4]:
            self._q[4] = x
            k = 3
        else:
            k = 0
            for i in range(4):
                if self._q[i] <= x < self._q[i + 1]:
                    k = i
                    break
        for i in range(k + 1, 5):
            self._n[i] += 1
        for i in range(5):
            self._np[i] += self._dn[i]
        for i in range(1, 4):
            d = self._np[i] - self._n[i]
            if (d >= 1 and self._n[i + 1] - self._n[i] > 1) or (
                d <= -1 and self._n[i - 1] - self._n[i] < -1
            ):
                d_sign = 1 if d >= 0 else -1
                q_par = self._parabolic(i, d_sign)
                if self._q[i - 1] < q_par < self._q[i + 1]:
                    self._q[i] = q_par
                else:
                    self._q[i] = self._linear(i, d_sign)
                self._n[i] += d_sign

    def _parabolic(self, i: int, d: int) -> float:
        n = self._n
        q = self._q
        return q[i] + d / (n[i + 1] - n[i - 1]) * (
            (n[i] - n[i - 1] + d) * (q[i + 1] - q[i]) / (n[i + 1] - n[i])
            + (n[i + 1] - n[i] - d) * (q[i] - q[i - 1]) / (n[i] - n[i - 1])
        )

    def _linear(self, i: int, d: int) -> float:
        return self._q[i] + d * (self._q[i + d] - self._q[i]) / (self._n[i + d] - self._n[i])

    @property
    def value(self) -> float:
        """Current quantile estimate (``0.0`` before any observation)."""
        if not self._q:
            return 0.0
        if len(self._q) < 5:
            idx = min(len(self._q) - 1, int(self.p * len(self._q)))
            return sorted(self._q)[idx]
        return self._q[2]


@dataclass
class RouterStats:
    """Mutable counters + quantile sketches for one router (cheap to snapshot)."""

    admitted: int = 0
    rejected: int = 0
    expired: int = 0
    coalesced: int = 0
    dispatched: int = 0
    succeeded: int = 0
    failed: int = 0
    cancelled: int = 0
    preempted: int = 0
    cache_hits: int = 0
    batches: int = 0
    batched_requests: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    rejects_by_reason: dict[str, int] = field(default_factory=dict)
    served_by_priority: dict[str, int] = field(default_factory=dict)
    _wait_p50: P2Quantile = field(default_factory=lambda: P2Quantile(0.50))
    _wait_p99: P2Quantile = field(default_factory=lambda: P2Quantile(0.99))

    # -- record ----------------------------------------------------------- #

    def on_admit(self) -> None:
        self.admitted += 1

    def on_reject(self, reason: RejectReason) -> None:
        self.rejected += 1
        self.rejects_by_reason[reason.value] = self.rejects_by_reason.get(reason.value, 0) + 1

    def on_expire(self) -> None:
        self.expired += 1

    def on_cancel(self) -> None:
        self.cancelled += 1

    def on_preempt(self) -> None:
        self.preempted += 1

    def on_coalesce(self, n: int = 1) -> None:
        self.coalesced += n

    def on_dispatch(self, request_priority: RequestPriority, wait_s: float) -> None:
        self.dispatched += 1
        name = request_priority.name
        self.served_by_priority[name] = self.served_by_priority.get(name, 0) + 1
        self._wait_p50.observe(wait_s)
        self._wait_p99.observe(wait_s)

    def on_batch(self, size: int) -> None:
        self.batches += 1
        self.batched_requests += size

    def on_complete(self, *, ok: bool, tokens_in: int, tokens_out: int, cache_hit: bool) -> None:
        if ok:
            self.succeeded += 1
        else:
            self.failed += 1
        if cache_hit:
            self.cache_hits += 1
        self.tokens_in += tokens_in
        self.tokens_out += tokens_out

    # -- derive ----------------------------------------------------------- #

    @property
    def wait_p50_s(self) -> float:
        return self._wait_p50.value

    @property
    def wait_p99_s(self) -> float:
        return self._wait_p99.value

    @property
    def avg_batch_size(self) -> float:
        return self.batched_requests / self.batches if self.batches else 0.0

    @property
    def cache_hit_rate(self) -> float:
        total = self.dispatched + self.coalesced
        return self.coalesced / total if total else 0.0

    @property
    def reject_rate(self) -> float:
        offered = self.admitted + self.rejected
        return self.rejected / offered if offered else 0.0

    def snapshot(self) -> dict[str, float | int | dict[str, int]]:
        """A flat, log-/JSON-safe snapshot for the metrics panel + SLO controller."""
        return {
            "admitted": self.admitted,
            "rejected": self.rejected,
            "expired": self.expired,
            "coalesced": self.coalesced,
            "dispatched": self.dispatched,
            "succeeded": self.succeeded,
            "failed": self.failed,
            "cancelled": self.cancelled,
            "preempted": self.preempted,
            "cache_hits": self.cache_hits,
            "batches": self.batches,
            "avg_batch_size": round(self.avg_batch_size, 3),
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "wait_p50_s": round(self.wait_p50_s, 4),
            "wait_p99_s": round(self.wait_p99_s, 4),
            "reject_rate": round(self.reject_rate, 4),
            "cache_hit_rate": round(self.cache_hit_rate, 4),
            "rejects_by_reason": dict(self.rejects_by_reason),
            "served_by_priority": dict(self.served_by_priority),
        }


__all__ = ["P2Quantile", "RouterStats"]
