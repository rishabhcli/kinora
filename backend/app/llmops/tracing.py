"""Structured run-tracing — prompt + inputs + outputs + tokens + cost, queryable.

Every LLM call the platform observes becomes a :class:`RunTrace`: which prompt
key + version produced it, the model, a (redacted-friendly) snapshot of inputs
and outputs, token counts, USD cost, latency, the guardrail decision, and loose
attribution (book / session). Traces feed a query API so an operator can ask "how
much did the Cinematographer cost on book X this week, and what was its p95
latency?" without a separate observability stack.

* :class:`RunTrace` — the immutable record (JSON-serializable).
* :class:`TraceQuery` — a filter (by key/version/model/book/session/time/min-cost)
  + ordering + limit.
* :class:`TraceStore` (protocol) and :class:`InMemoryTraceStore` — the default,
  in-process store with a bounded ring buffer; :mod:`app.llmops.store` adds the
  DB-backed store over ``llmops_runs``.
* :func:`aggregate` — rolls a list of traces into totals + percentiles
  (count, total/avg tokens + cost, p50/p95 latency), grouped by a chosen key.

The token/cost math reuses the *physical* unit philosophy (tokens in/out) and a
per-1k price taken from a :class:`~app.llmops.models_registry.ModelRegistry` when
available, so a trace's cost agrees with the model registry. Pure + deterministic.
"""

from __future__ import annotations

import statistics
import uuid
from collections import deque
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Protocol

from app.llmops.models_registry import ModelRegistry


def _now() -> datetime:
    return datetime.now(UTC)


def new_trace_id() -> str:
    return f"trace_{uuid.uuid4().hex[:16]}"


@dataclass(frozen=True, slots=True)
class RunTrace:
    """One observed LLM call."""

    id: str
    prompt_key: str
    prompt_version: str
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: Decimal
    latency_ms: float
    created_at: datetime
    inputs: dict[str, Any] = field(default_factory=dict)
    output: str = ""
    guardrail_decision: str | None = None
    book_id: str | None = None
    session_id: str | None = None
    cache_hit: bool = False
    error: str | None = None

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["cost_usd"] = str(self.cost_usd)
        d["created_at"] = self.created_at.isoformat()
        d["total_tokens"] = self.total_tokens
        return d


def cost_of(
    model: str, input_tokens: int, output_tokens: int, *, registry: ModelRegistry | None
) -> Decimal:
    """USD cost of a call from the model registry's price table (0 if unknown)."""
    if registry is None or not registry.has(model):
        return Decimal("0")
    card = registry.get(model)
    return card.input_per_1k * Decimal(input_tokens) / Decimal(1000) + card.output_per_1k * Decimal(
        output_tokens
    ) / Decimal(1000)


@dataclass(frozen=True, slots=True)
class TraceQuery:
    """A filter over traces (all fields optional ⇒ match-all)."""

    prompt_key: str | None = None
    prompt_version: str | None = None
    model: str | None = None
    book_id: str | None = None
    session_id: str | None = None
    since: datetime | None = None
    until: datetime | None = None
    min_cost_usd: Decimal | None = None
    errors_only: bool = False
    cache_hits_only: bool | None = None
    limit: int | None = None
    newest_first: bool = True

    def all_matching(self) -> TraceQuery:
        """A copy with ``limit`` cleared — for complete aggregates over the filter."""
        return replace(self, limit=None)

    def matches(self, t: RunTrace) -> bool:
        if self.prompt_key is not None and t.prompt_key != self.prompt_key:
            return False
        if self.prompt_version is not None and t.prompt_version != self.prompt_version:
            return False
        if self.model is not None and t.model != self.model:
            return False
        if self.book_id is not None and t.book_id != self.book_id:
            return False
        if self.session_id is not None and t.session_id != self.session_id:
            return False
        if self.since is not None and t.created_at < self.since:
            return False
        if self.until is not None and t.created_at > self.until:
            return False
        if self.min_cost_usd is not None and t.cost_usd < self.min_cost_usd:
            return False
        if self.errors_only and t.error is None:
            return False
        return not (
            self.cache_hits_only is not None and t.cache_hit != self.cache_hits_only
        )


class TraceStore(Protocol):
    """A place run traces are recorded and queried."""

    def record(self, trace: RunTrace) -> None: ...

    def query(self, q: TraceQuery) -> list[RunTrace]: ...

    def get(self, trace_id: str) -> RunTrace | None: ...


@dataclass
class InMemoryTraceStore:
    """A bounded, in-process trace store (a ring buffer)."""

    capacity: int = 10_000
    _traces: deque[RunTrace] = field(default_factory=lambda: deque(maxlen=10_000))
    _by_id: dict[str, RunTrace] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Re-bind the deque to the requested capacity.
        self._traces = deque(self._traces, maxlen=self.capacity)

    def record(self, trace: RunTrace) -> None:
        if len(self._traces) == self._traces.maxlen and self._traces:
            evicted = self._traces[0]
            self._by_id.pop(evicted.id, None)
        self._traces.append(trace)
        self._by_id[trace.id] = trace

    def query(self, q: TraceQuery) -> list[RunTrace]:
        results = [t for t in self._traces if q.matches(t)]
        results.sort(key=lambda t: t.created_at, reverse=q.newest_first)
        if q.limit is not None:
            results = results[: q.limit]
        return results

    def get(self, trace_id: str) -> RunTrace | None:
        return self._by_id.get(trace_id)

    def __len__(self) -> int:
        return len(self._traces)


@dataclass(frozen=True, slots=True)
class TraceAggregate:
    """Rolled-up stats over a set of traces."""

    count: int
    total_input_tokens: int
    total_output_tokens: int
    total_cost_usd: Decimal
    avg_latency_ms: float
    p50_latency_ms: float
    p95_latency_ms: float
    error_count: int
    cache_hit_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "count": self.count,
            "total_input_tokens": self.total_input_tokens,
            "total_output_tokens": self.total_output_tokens,
            "total_tokens": self.total_input_tokens + self.total_output_tokens,
            "total_cost_usd": str(self.total_cost_usd),
            "avg_latency_ms": self.avg_latency_ms,
            "p50_latency_ms": self.p50_latency_ms,
            "p95_latency_ms": self.p95_latency_ms,
            "error_count": self.error_count,
            "cache_hit_count": self.cache_hit_count,
            "cache_hit_rate": round(self.cache_hit_count / self.count, 6) if self.count else 0.0,
        }


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return round(ordered[0], 3)
    rank = pct / 100 * (len(ordered) - 1)
    lo = int(rank)
    hi = min(lo + 1, len(ordered) - 1)
    frac = rank - lo
    return round(ordered[lo] + (ordered[hi] - ordered[lo]) * frac, 3)


def aggregate(traces: Iterable[RunTrace]) -> TraceAggregate:
    """Roll a set of traces into totals + latency percentiles."""
    traces = list(traces)
    latencies = [t.latency_ms for t in traces]
    return TraceAggregate(
        count=len(traces),
        total_input_tokens=sum(t.input_tokens for t in traces),
        total_output_tokens=sum(t.output_tokens for t in traces),
        total_cost_usd=sum((t.cost_usd for t in traces), Decimal("0")),
        avg_latency_ms=round(statistics.fmean(latencies), 3) if latencies else 0.0,
        p50_latency_ms=_percentile(latencies, 50),
        p95_latency_ms=_percentile(latencies, 95),
        error_count=sum(1 for t in traces if t.error is not None),
        cache_hit_count=sum(1 for t in traces if t.cache_hit),
    )


def group_by(traces: Iterable[RunTrace], key: str) -> dict[str, TraceAggregate]:
    """Group traces by an attribute (``prompt_key``/``model``/``book_id``/…) and aggregate each."""
    buckets: dict[str, list[RunTrace]] = {}
    for t in traces:
        bucket = str(getattr(t, key, None))
        buckets.setdefault(bucket, []).append(t)
    return {k: aggregate(v) for k, v in buckets.items()}


__all__ = [
    "InMemoryTraceStore",
    "RunTrace",
    "TraceAggregate",
    "TraceQuery",
    "TraceStore",
    "aggregate",
    "cost_of",
    "group_by",
    "new_trace_id",
]
