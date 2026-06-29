"""Detector behaviour over synthetic attack traces (Milestone 2)."""

from __future__ import annotations

from app.zerotrust.defense.clock import ManualClock
from app.zerotrust.defense.detectors.behavioral import BehavioralConfig, BehavioralDetector
from app.zerotrust.defense.detectors.credential_stuffing import (
    CredentialStuffingConfig,
    CredentialStuffingDetector,
)
from app.zerotrust.defense.detectors.scraping import (
    ScrapingConfig,
    ScrapingDetector,
    ua_suspicion,
)
from app.zerotrust.defense.detectors.sequence import SequenceAnomalyDetector, SequenceConfig
from app.zerotrust.defense.detectors.takeover import AccountTakeoverDetector, TakeoverConfig
from app.zerotrust.defense.types import AuthOutcome, EventKind, SecurityEvent, ThreatCategory

from . import traces


def _drive(detector, events: list[SecurityEvent], clock: ManualClock) -> list:
    out = []
    for ev in events:
        clock.at(ev.ts)
        out.extend(list(detector.observe(ev)))
    return out


# --------------------------------------------------------------------------- #
# Credential stuffing
# --------------------------------------------------------------------------- #


def test_credential_stuffing_fires_on_fanout() -> None:
    clk = ManualClock()
    det = CredentialStuffingDetector(
        config=CredentialStuffingConfig(window=120.0, distinct_user_threshold=12, min_attempts=12),
        clock=clk,
    )
    trace = traces.credential_stuffing(start=0.0, spacing=0.3)
    alerts = _drive(det, trace, clk)
    assert alerts, "many distinct usernames from one ip must alert"
    assert alerts[-1].category is ThreatCategory.CREDENTIAL_STUFFING
    assert alerts[-1].evidence_get("distinct_usernames") >= 12


def test_credential_stuffing_escalates_on_success() -> None:
    clk = ManualClock()
    det = CredentialStuffingDetector(clock=clk)
    trace = traces.credential_stuffing(start=0.0, spacing=0.3, success_at=40)
    alerts = _drive(det, trace, clk)
    assert alerts
    # A confirmed valid pair pushes the score to the critical band + block action.
    top = max(alerts, key=lambda a: a.score)
    assert top.score >= 0.9
    assert top.recommended_action == "block_ip"


def test_credential_stuffing_quiet_on_single_user_brute_force() -> None:
    # Hammering ONE username is brute force, not stuffing — fan-out stays at 1.
    clk = ManualClock()
    det = CredentialStuffingDetector(clock=clk)
    trace = traces.brute_force(start=0.0, n=60, spacing=0.2)
    alerts = _drive(det, trace, clk)
    assert alerts == []


def test_credential_stuffing_config_validation() -> None:
    import pytest

    with pytest.raises(ValueError):
        CredentialStuffingConfig(window=0)
    with pytest.raises(ValueError):
        CredentialStuffingConfig(distinct_user_threshold=1)


# --------------------------------------------------------------------------- #
# Account takeover
# --------------------------------------------------------------------------- #


def test_takeover_flags_new_context_after_history() -> None:
    clk = ManualClock()
    det = AccountTakeoverDetector(clock=clk)
    trace = traces.takeover_session(start=0.0)
    alerts = _drive(det, trace, clk)
    assert len(alerts) == 1
    a = alerts[0]
    assert a.category is ThreatCategory.ACCOUNT_TAKEOVER
    assert a.subject == "carol"
    assert a.evidence_get("new_ip") is True
    assert a.evidence_get("new_device") is True


def test_takeover_quiet_for_consistent_user() -> None:
    clk = ManualClock()
    det = AccountTakeoverDetector(clock=clk)
    trace = traces.benign_logins(start=0.0, user="dave", ip="10.0.0.7", n=20)
    alerts = _drive(det, trace, clk)
    assert alerts == []


def test_takeover_never_flags_first_login() -> None:
    clk = ManualClock()
    det = AccountTakeoverDetector(clock=clk)
    first = SecurityEvent.auth(
        ts=0.0, source_ip="1.2.3.4", username="new", outcome=AuthOutcome.SUCCESS, principal="new"
    )
    assert list(det.observe(first)) == []


def test_takeover_impossible_travel() -> None:
    clk = ManualClock()
    det = AccountTakeoverDetector(config=TakeoverConfig(max_travel_kmh=900.0), clock=clk)
    # Establish baseline in New York.
    ny = SecurityEvent.auth(
        ts=0.0,
        source_ip="10.0.0.1",
        username="erin",
        outcome=AuthOutcome.SUCCESS,
        principal="erin",
        user_agent="UA",
        meta={"geo_lat": 40.7, "geo_lon": -74.0},
    )
    clk.at(0.0)
    det.observe(ny)
    # 10 minutes later, a login from Tokyo (same ip/device but ~10800 km away).
    tokyo = SecurityEvent.auth(
        ts=600.0,
        source_ip="10.0.0.1",
        username="erin",
        outcome=AuthOutcome.SUCCESS,
        principal="erin",
        user_agent="UA",
        meta={"geo_lat": 35.7, "geo_lon": 139.7},
    )
    clk.at(600.0)
    alerts = list(det.observe(tokyo))
    assert alerts
    assert alerts[0].evidence_get("impossible_travel") is True
    assert alerts[0].score >= 0.9


def test_takeover_anonymous_failures_ignored() -> None:
    clk = ManualClock()
    det = AccountTakeoverDetector(clock=clk)
    ev = SecurityEvent.auth(
        ts=0.0, source_ip="1.2.3.4", username="x", outcome=AuthOutcome.FAILURE
    )  # no principal
    assert list(det.observe(ev)) == []


# --------------------------------------------------------------------------- #
# Scraping
# --------------------------------------------------------------------------- #


def test_scraping_fires_on_breadth() -> None:
    clk = ManualClock()
    det = ScrapingDetector(
        config=ScrapingConfig(window=60.0, distinct_path_threshold=30, min_requests=30), clock=clk
    )
    trace = traces.scraping_walk(start=0.0, n=120, spacing=0.25)
    alerts = _drive(det, trace, clk)
    assert alerts
    a = max(alerts, key=lambda x: x.score)
    assert a.category is ThreatCategory.SCRAPING
    assert a.evidence_get("distinct_paths") >= 30
    # Robotic cadence (fixed spacing) + scripted UA => high score.
    assert a.evidence_get("regular_cadence") is True
    assert a.score >= 0.8


def test_scraping_quiet_for_single_book_reader() -> None:
    clk = ManualClock()
    det = ScrapingDetector(clock=clk)
    # A reader re-hitting the same few paths for one book, irregular timing.
    out = []
    t = 0.0
    paths = ["/api/books/7", "/api/books/7/page/1", "/api/books/7/page/2"]
    for i in range(60):
        ev = SecurityEvent.access(
            ts=t,
            source_ip="10.0.0.9",
            principal="reader",
            target=paths[i % len(paths)],
            user_agent="Mozilla/5.0 (Macintosh)",
        )
        clk.at(t)
        out.extend(list(det.observe(ev)))
        t += 1.0 + (i % 5)  # irregular human-ish gaps
    assert out == []


def test_ua_suspicion_levels() -> None:
    assert ua_suspicion("Mozilla/5.0 Safari") == 0.0
    assert ua_suspicion("curl/8.4") == 1.0
    assert ua_suspicion(None) == 0.8
    assert ua_suspicion("SomeCustomClient/1.0") == 0.5


# --------------------------------------------------------------------------- #
# Sequence anomaly
# --------------------------------------------------------------------------- #


def test_sequence_anomaly_flags_unusual_order() -> None:
    clk = ManualClock()
    det = SequenceAnomalyDetector(
        config=SequenceConfig(surprise_threshold=8.0, min_prior=10), clock=clk
    )
    # Teach a strong normal pattern: read -> read -> read ...
    t = 0.0
    for _ in range(60):
        clk.at(t)
        det.observe(
            SecurityEvent.access(ts=t, source_ip="10.0.0.5", principal="frank", target="x", action="read")
        )
        t += 1.0
    # Now a wildly out-of-distribution burst of never-seen privileged actions.
    alerts = []
    for action in ["export_all", "change_email", "delete_account", "payout", "export_all", "payout"]:
        clk.at(t)
        alerts.extend(
            list(
                det.observe(
                    SecurityEvent.audit(ts=t, source_ip="10.0.0.5", principal="frank", action=action)
                )
            )
        )
        t += 0.5
    assert alerts
    assert alerts[0].category is ThreatCategory.SEQUENCE_ANOMALY


def test_sequence_anomaly_quiet_on_learned_pattern() -> None:
    clk = ManualClock()
    det = SequenceAnomalyDetector(config=SequenceConfig(min_prior=10), clock=clk)
    t = 0.0
    alerts = []
    for i in range(200):
        action = "read" if i % 2 == 0 else "scroll"
        clk.at(t)
        alerts.extend(
            list(
                det.observe(
                    SecurityEvent.access(
                        ts=t, source_ip="10.0.0.5", principal="gina", target="x", action=action
                    )
                )
            )
        )
        t += 1.0
    assert alerts == []


# --------------------------------------------------------------------------- #
# Behavioral (isolation forest)
# --------------------------------------------------------------------------- #


def _benign_vectors(n: int = 300) -> list[list[float]]:
    import random

    rng = random.Random(11)
    out = []
    for _ in range(n):
        # Benign profile: low rate, few targets, no failures/errors, one agent.
        out.append(
            [
                rng.uniform(0.0, 0.2),  # rate
                rng.uniform(1.0, 4.0),  # distinct targets
                0.0,  # failure ratio
                1.0,  # distinct agents
                rng.uniform(0.0, 0.05),  # error ratio
                rng.uniform(0.05, 0.3),  # breadth ratio
            ]
        )
    return out


def test_behavioral_unfitted_is_silent() -> None:
    clk = ManualClock()
    det = BehavioralDetector(config=BehavioralConfig(min_events=5), clock=clk)
    assert not det.fitted
    out = []
    t = 0.0
    for _ in range(30):
        clk.at(t)
        out.extend(
            list(det.observe(SecurityEvent.access(ts=t, source_ip="1.1.1.1", principal="h", target="x")))
        )
        t += 1.0
    assert out == []  # never fits implicitly


def test_behavioral_flags_outlier_profile() -> None:
    clk = ManualClock()
    det = BehavioralDetector(
        config=BehavioralConfig(window=60.0, min_events=10, score_threshold=0.6), clock=clk
    )
    det.fit(_benign_vectors())
    # An attacker: very high rate, many distinct targets, high failure ratio.
    out = []
    t = 0.0
    for i in range(40):
        clk.at(t)
        ev = SecurityEvent.access(
            ts=t,
            source_ip="6.6.6.6",
            principal="attacker",
            target=f"/r/{i}",
            status_code=403,
            user_agent=f"bot-{i}",
        )
        out.extend(list(det.observe(ev)))
        t += 0.05  # extreme rate
    assert out, "an out-of-distribution behaviour vector should be flagged"
    assert out[-1].category is ThreatCategory.BEHAVIORAL_ANOMALY


def test_behavioral_config_validation() -> None:
    import pytest

    with pytest.raises(ValueError):
        BehavioralConfig(window=0)
    with pytest.raises(ValueError):
        BehavioralConfig(min_events=0)


def test_kinds_prefilter() -> None:
    # Each detector only consumes the kinds it declares.
    clk = ManualClock()
    cs = CredentialStuffingDetector(clock=clk)
    http_ev = SecurityEvent(kind=EventKind.HTTP, ts=0.0, source_ip="1.1.1.1")
    assert cs.consumes(http_ev) is False
