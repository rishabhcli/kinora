"""Unit tests for dedup/rollup (defense.alerting) and the store seam."""

from __future__ import annotations

from app.zerotrust.defense.alerting import DedupConfig, Deduper
from app.zerotrust.defense.store import InMemoryAlertStore, NullAlertSink
from app.zerotrust.defense.types import Alert, Severity, ThreatCategory


def _alert(score: float = 0.8, ts: float = 0.0, key: str = "k") -> Alert:
    return Alert(
        detector="d",
        category=ThreatCategory.RATE_ANOMALY,
        severity=Severity.for_score(score),
        score=score,
        subject="1.2.3.4",
        source_ip="1.2.3.4",
        ts=ts,
        title="t",
        dedup_key=key,
    )


def test_deduper_emits_first_then_suppresses() -> None:
    d = Deduper(DedupConfig(cooldown=30.0))
    first = d.admit(_alert(ts=0.0), now=0.0)
    assert first is not None and first.count == 1
    # Within cooldown -> suppressed.
    assert d.admit(_alert(ts=1.0), now=1.0) is None
    assert d.admit(_alert(ts=2.0), now=2.0) is None
    # After cooldown -> re-emit with accumulated count.
    rolled = d.admit(_alert(ts=31.0), now=31.0)
    assert rolled is not None
    assert rolled.count == 4
    assert rolled.last_seen == 31.0
    assert rolled.first_seen == 0.0


def test_deduper_escalates_on_severity() -> None:
    d = Deduper(DedupConfig(cooldown=1000.0, escalate_on_severity=True))
    assert d.admit(_alert(score=0.3, ts=0.0), now=0.0) is not None  # LOW, first
    # Still within cooldown but severity rose CRITICAL -> immediate re-emit.
    out = d.admit(_alert(score=0.95, ts=1.0), now=1.0)
    assert out is not None
    assert out.severity is Severity.CRITICAL


def test_deduper_resets_after_idle() -> None:
    d = Deduper(DedupConfig(cooldown=10.0, reset_after=100.0))
    assert d.admit(_alert(ts=0.0), now=0.0) is not None
    # Long idle -> next is a fresh incident (count resets to 1).
    fresh = d.admit(_alert(ts=500.0), now=500.0)
    assert fresh is not None and fresh.count == 1


def test_store_ring_and_query() -> None:
    store = InMemoryAlertStore(capacity=100)
    store.record(_alert(score=0.9, ts=1.0, key="a"))
    store.record(_alert(score=0.3, ts=2.0, key="b"))
    assert len(store) == 2
    high = store.query(min_severity=Severity.HIGH)
    assert len(high) == 1
    assert store.latest("a") is not None
    assert store.query(category=ThreatCategory.RATE_ANOMALY)
    assert store.query(source_ip="1.2.3.4")
    assert store.query(since=1.5) == [a for a in store if a.last_seen >= 1.5]


def test_store_eviction_keeps_total() -> None:
    store = InMemoryAlertStore(capacity=3)
    for i in range(10):
        store.record(_alert(ts=float(i), key=f"k{i}"))
    assert len(store) == 3
    assert store.total_recorded == 10


def test_store_top_subjects_and_categories() -> None:
    store = InMemoryAlertStore()
    store.record(_alert(key="a"))
    store.record(_alert(key="b"))
    top = store.top_subjects(limit=5)
    assert top[0][0] == "1.2.3.4"
    assert store.categories()["rate_anomaly"] == 2


def test_null_sink_is_inert() -> None:
    sink = NullAlertSink()
    assert sink.record(_alert()) is None
