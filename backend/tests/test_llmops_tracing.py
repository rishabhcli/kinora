"""Unit tests for structured run tracing + the query/aggregate API (no infra)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.llmops.models_registry import default_catalog
from app.llmops.tracing import (
    InMemoryTraceStore,
    RunTrace,
    TraceQuery,
    aggregate,
    cost_of,
    group_by,
    new_trace_id,
)


def _trace(**kw: object) -> RunTrace:
    base = {
        "id": new_trace_id(),
        "prompt_key": "adapter",
        "prompt_version": "1.0.0",
        "model": "qwen3.7-plus",
        "input_tokens": 1000,
        "output_tokens": 100,
        "cost_usd": Decimal("0.001"),
        "latency_ms": 100.0,
        "created_at": datetime.now(UTC),
    }
    base.update(kw)
    return RunTrace(**base)  # type: ignore[arg-type]


def test_cost_of_from_registry() -> None:
    reg = default_catalog()
    # qwen3.7-plus: 0.0008/1k in, 0.0020/1k out.
    cost = cost_of("qwen3.7-plus", 1000, 1000, registry=reg)
    assert cost == Decimal("0.0008") + Decimal("0.0020")
    assert cost_of("unknown-model", 1000, 1000, registry=reg) == Decimal("0")


def test_record_and_get() -> None:
    store = InMemoryTraceStore()
    t = _trace()
    store.record(t)
    assert store.get(t.id) is t
    assert store.get("missing") is None
    assert len(store) == 1


def test_query_filters() -> None:
    store = InMemoryTraceStore()
    store.record(_trace(book_id="b1", model="qwen3.7-plus"))
    store.record(_trace(book_id="b2", model="qwen-vl-max"))
    store.record(_trace(book_id="b1", model="qwen-vl-max", error="boom"))
    assert len(store.query(TraceQuery(book_id="b1"))) == 2
    assert len(store.query(TraceQuery(model="qwen-vl-max"))) == 2
    assert len(store.query(TraceQuery(errors_only=True))) == 1


def test_query_limit_and_order() -> None:
    store = InMemoryTraceStore()
    now = datetime.now(UTC)
    for i in range(5):
        store.record(_trace(created_at=now + timedelta(seconds=i)))
    newest = store.query(TraceQuery(limit=2))
    assert len(newest) == 2
    assert newest[0].created_at > newest[1].created_at  # newest_first default


def test_ring_buffer_eviction() -> None:
    store = InMemoryTraceStore(capacity=3)
    ids = []
    for _ in range(5):
        t = _trace()
        ids.append(t.id)
        store.record(t)
    assert len(store) == 3
    assert store.get(ids[0]) is None  # evicted
    assert store.get(ids[-1]) is not None


def test_aggregate_totals_and_percentiles() -> None:
    traces = [_trace(latency_ms=float(x), cost_usd=Decimal("0.001")) for x in (10, 20, 30, 40, 100)]
    agg = aggregate(traces)
    assert agg.count == 5
    assert agg.total_cost_usd == Decimal("0.005")
    assert agg.p50_latency_ms == 30.0
    assert agg.p95_latency_ms > agg.p50_latency_ms
    assert agg.to_dict()["total_tokens"] == 5 * 1100


def test_aggregate_cache_hit_rate() -> None:
    traces = [_trace(cache_hit=True), _trace(cache_hit=False), _trace(cache_hit=False)]
    agg = aggregate(traces)
    assert agg.cache_hit_count == 1
    assert agg.to_dict()["cache_hit_rate"] == round(1 / 3, 6)


def test_group_by_model() -> None:
    store = InMemoryTraceStore()
    store.record(_trace(model="a"))
    store.record(_trace(model="a"))
    store.record(_trace(model="b"))
    grouped = group_by(store.query(TraceQuery()), "model")
    assert grouped["a"].count == 2
    assert grouped["b"].count == 1


def test_all_matching_clears_limit() -> None:
    q = TraceQuery(prompt_key="adapter", limit=5)
    assert q.all_matching().limit is None
    assert q.all_matching().prompt_key == "adapter"


def test_to_dict_serializable() -> None:
    d = _trace().to_dict()
    assert isinstance(d["cost_usd"], str)
    assert isinstance(d["created_at"], str)
    assert "total_tokens" in d
