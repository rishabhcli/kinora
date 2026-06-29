"""Unit tests for the core value types and the clock seam."""

from __future__ import annotations

import pytest

from app.zerotrust.defense.clock import ManualClock, SystemClock
from app.zerotrust.defense.types import (
    Alert,
    AuthOutcome,
    EventKind,
    SecurityEvent,
    Severity,
    ThreatCategory,
    make_evidence,
)


def test_severity_banding() -> None:
    assert Severity.for_score(0.95) is Severity.CRITICAL
    assert Severity.for_score(0.75) is Severity.HIGH
    assert Severity.for_score(0.5) is Severity.MEDIUM
    assert Severity.for_score(0.2) is Severity.LOW
    assert Severity.for_score(0.0) is Severity.INFO
    assert Severity.HIGH > Severity.LOW
    assert Severity.HIGH.label == "high"


def test_event_factories_set_kind_and_subject() -> None:
    a = SecurityEvent.auth(
        ts=1.0, source_ip="1.2.3.4", username="bob", outcome=AuthOutcome.FAILURE
    )
    assert a.kind is EventKind.AUTH
    assert a.target == "bob"
    assert a.subject == "1.2.3.4"  # no principal -> falls back to ip

    acc = SecurityEvent.access(
        ts=2.0, source_ip="1.2.3.4", principal="bob", target="/x", status_code=403
    )
    assert acc.kind is EventKind.ACCESS
    assert acc.subject == "bob"

    aud = SecurityEvent.audit(ts=3.0, source_ip="1.2.3.4", principal="bob", action="delete")
    assert aud.kind is EventKind.AUDIT
    assert aud.action == "delete"


def test_event_meta_is_frozen_and_queryable() -> None:
    ev = SecurityEvent.auth(
        ts=1.0,
        source_ip="1.2.3.4",
        username="bob",
        outcome=AuthOutcome.SUCCESS,
        meta={"geo": "US", "asn": 1234},
    )
    assert ev.get("geo") == "US"
    assert ev.get("missing", "def") == "def"
    # meta is a sorted tuple of pairs (hashable + deterministic).
    assert ev.meta == (("asn", 1234), ("geo", "US"))
    merged = ev.with_meta(geo="DE")
    assert merged.get("geo") == "DE"
    assert ev.get("geo") == "US"  # original untouched (immutable)


def test_alert_post_init_defaults() -> None:
    a = Alert(
        detector="d",
        category=ThreatCategory.RATE_ANOMALY,
        severity=Severity.HIGH,
        score=0.8,
        subject="1.2.3.4",
        ts=10.0,
        title="t",
    )
    assert a.first_seen == 10.0
    assert a.last_seen == 10.0
    assert a.dedup_key == "d:rate_anomaly:1.2.3.4"
    d = a.as_dict()
    assert d["severity"] == "high"
    assert d["severity_rank"] == int(Severity.HIGH)


def test_alert_first_seen_zero_is_preserved() -> None:
    # Regression: 0.0 is a *valid* timestamp and must not be treated as "unset".
    a = Alert(
        detector="d",
        category=ThreatCategory.RATE_ANOMALY,
        severity=Severity.HIGH,
        score=0.8,
        subject="x",
        ts=999.0,
        title="t",
        first_seen=0.0,
        last_seen=0.0,
    )
    assert a.first_seen == 0.0
    assert a.last_seen == 0.0
    assert a.first_at == 0.0
    assert a.last_at == 0.0


def test_alert_rejects_out_of_range_score() -> None:
    with pytest.raises(ValueError):
        Alert(
            detector="d",
            category=ThreatCategory.BOT,
            severity=Severity.LOW,
            score=1.5,
            subject="x",
            ts=0.0,
            title="t",
        )


def test_make_evidence_is_sorted() -> None:
    ev = make_evidence(z=1, a=2)
    assert ev == (("a", 2), ("z", 1))


def test_manual_clock_advances_together_and_independently() -> None:
    c = ManualClock(wall=1000.0, mono=0.0)
    c.advance(5.0)
    assert c.wall() == 1005.0
    assert c.mono() == 5.0
    c.step_wall(-2.0)  # NTP step backwards: wall only
    assert c.wall() == 1003.0
    assert c.mono() == 5.0
    with pytest.raises(ValueError):
        c.advance(-1.0)


def test_system_clock_is_monotone() -> None:
    c = SystemClock()
    assert c.mono() <= c.mono()
    assert c.wall() > 0
