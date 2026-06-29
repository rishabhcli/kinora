"""Unit tests for the rate-anomaly detector (defense.detectors.rate)."""

from __future__ import annotations

from app.zerotrust.defense.clock import ManualClock
from app.zerotrust.defense.detectors.rate import RateAnomalyDetector, RateConfig
from app.zerotrust.defense.types import AuthOutcome, SecurityEvent, ThreatCategory


def _auth(clock: ManualClock, ip: str = "1.2.3.4") -> SecurityEvent:
    return SecurityEvent.auth(
        ts=clock.wall(), source_ip=ip, username="u", outcome=AuthOutcome.FAILURE
    )


def test_absolute_floor_fires_immediately() -> None:
    clk = ManualClock()
    det = RateAnomalyDetector(config=RateConfig(window=60.0, absolute_floor=5), clock=clk)
    alerts = []
    for _ in range(5):
        alerts.extend(list(det.observe(_auth(clk))))
        clk.advance(0.1)
    assert len(alerts) == 1
    a = alerts[0]
    assert a.category is ThreatCategory.RATE_ANOMALY
    assert a.evidence_get("reason") == "absolute_floor"
    assert a.evidence_get("window_count") == 5
    assert a.recommended_action == "rate_limit"


def test_below_floor_is_quiet() -> None:
    clk = ManualClock()
    det = RateAnomalyDetector(config=RateConfig(window=60.0, absolute_floor=100), clock=clk)
    alerts = []
    for _ in range(20):
        alerts.extend(list(det.observe(_auth(clk))))
        clk.advance(0.1)
    assert alerts == []


def test_baseline_anomaly_after_learning() -> None:
    clk = ManualClock()
    det = RateAnomalyDetector(
        config=RateConfig(
            window=10.0,
            baseline_half_life=120.0,
            absolute_floor=0,
            min_observations=4,
            score_threshold=0.5,
        ),
        clock=clk,
    )
    # Learn a calm baseline: ~1 event per window for many windows.
    for _ in range(8):
        det.observe(_auth(clk))
        clk.advance(10.0)  # one per window
    # Now a sudden burst within a single window.
    burst_alerts = []
    for _ in range(40):
        burst_alerts.extend(list(det.observe(_auth(clk))))
        clk.advance(0.05)
    assert burst_alerts, "a burst far above the learned baseline must alert"
    assert burst_alerts[-1].evidence_get("reason") == "baseline"


def test_per_key_isolation() -> None:
    clk = ManualClock()
    det = RateAnomalyDetector(config=RateConfig(window=60.0, absolute_floor=5), clock=clk)
    # ip A floods; ip B stays quiet — only A should alert.
    a_alerts = []
    for _ in range(6):
        a_alerts.extend(list(det.observe(_auth(clk, ip="9.9.9.9"))))
        clk.advance(0.1)
    b_alerts = list(det.observe(_auth(clk, ip="8.8.8.8")))
    assert a_alerts
    assert b_alerts == []


def test_sweep_drops_idle_state() -> None:
    clk = ManualClock()
    det = RateAnomalyDetector(
        config=RateConfig(window=10.0, absolute_floor=2, state_ttl=100.0), clock=clk
    )
    det.observe(_auth(clk))
    assert det._state  # internal: state exists
    clk.advance(200.0)
    det.sweep(clk.mono())
    assert not det._state  # swept after TTL


def test_config_validation() -> None:
    import pytest

    with pytest.raises(ValueError):
        RateConfig(window=0.0)
    with pytest.raises(ValueError):
        RateConfig(min_observations=0)
