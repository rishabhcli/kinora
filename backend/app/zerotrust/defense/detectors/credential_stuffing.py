"""Credential-stuffing detection.

Brute force hammers *one* account with many passwords; credential stuffing
replays a breach list — *many distinct accounts*, usually one attempt each, from
a small set of ips, with a high failure rate (the leaked pairs are mostly stale).
The discriminating signal is therefore **username fan-out per source**, not raw
request rate, combined with a high failure ratio. A spray that succeeds on a few
accounts is the most dangerous variant and scores highest.

This detector keys on the source ip, tracking within a sliding window:

* the count of *distinct usernames* attempted (the fan-out);
* the failure ratio over attempts; and
* whether any attempt *succeeded* (a confirmed valid pair = escalation).

It is intentionally complementary to the rate detector: a low-and-slow stuffing
run that never trips a rate floor still trips the fan-out threshold.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from ..clock import Clock
from ..stats import clamp01
from ..types import (
    Alert,
    AuthOutcome,
    EventKind,
    SecurityEvent,
    Severity,
    ThreatCategory,
    make_evidence,
)
from ..windows import DistinctWindow, SlidingCounter
from .base import DetectorBase


@dataclass(slots=True)
class CredentialStuffingConfig:
    """Tuning for :class:`CredentialStuffingDetector`."""

    window: float = 120.0
    """Sliding window (seconds) over which fan-out is measured."""
    distinct_user_threshold: int = 12
    """Distinct usernames from one ip within the window to suspect stuffing."""
    min_attempts: int = 12
    """Minimum attempts before scoring (avoids firing on a handful)."""
    failure_ratio_threshold: float = 0.6
    """Failure ratio above which the fan-out looks like a breach replay."""
    distinct_cap: int = 8192
    state_ttl: float = 3600.0

    def __post_init__(self) -> None:
        if self.window <= 0:
            raise ValueError("window must be positive")
        if self.distinct_user_threshold < 2:
            raise ValueError("distinct_user_threshold must be >= 2")


@dataclass(slots=True)
class _IPState:
    users: DistinctWindow
    attempts: SlidingCounter
    failures: SlidingCounter
    successes: SlidingCounter
    last_mono: float = 0.0


class CredentialStuffingDetector(DetectorBase):
    """Detect breach-list replay by username fan-out + failure ratio per ip."""

    name = "credential_stuffing"
    kinds = frozenset({EventKind.AUTH})

    def __init__(
        self,
        *,
        name: str = "credential_stuffing",
        config: CredentialStuffingConfig | None = None,
        clock: Clock | None = None,
    ) -> None:
        super().__init__(clock=clock)
        self.name = name
        self.config = config or CredentialStuffingConfig()
        self._state: dict[str, _IPState] = {}

    def _state_for(self, ip: str) -> _IPState:
        st = self._state.get(ip)
        if st is None:
            w = self.config.window
            st = _IPState(
                users=DistinctWindow(w, cap=self.config.distinct_cap),
                attempts=SlidingCounter(w),
                failures=SlidingCounter(w),
                successes=SlidingCounter(w),
            )
            self._state[ip] = st
        return st

    def _observe(self, event: SecurityEvent) -> Iterable[Alert]:
        ip = event.source_ip
        now = self.clock.mono()
        st = self._state_for(ip)
        st.last_mono = now

        username = event.target or "?"
        distinct = st.users.add(username, now)
        attempts = st.attempts.hit(now)
        if event.outcome is AuthOutcome.FAILURE:
            st.failures.hit(now)
        elif event.outcome is AuthOutcome.SUCCESS:
            st.successes.hit(now)

        failures = st.failures.count(now)
        successes = st.successes.count(now)
        cfg = self.config

        if attempts < cfg.min_attempts or distinct < cfg.distinct_user_threshold:
            return ()
        failure_ratio = failures / attempts if attempts else 0.0
        if failure_ratio < cfg.failure_ratio_threshold and successes == 0:
            return ()

        # Score grows with fan-out beyond threshold but a fan-out-only finding
        # stays below the escalation band; a *confirmed success* (a valid pair in
        # the breach list) is a hard escalation that strictly dominates it.
        fanout_term = clamp01(
            (distinct - cfg.distinct_user_threshold) / (cfg.distinct_user_threshold * 2)
        )
        base = 0.55 + 0.30 * fanout_term  # fan-out-only tops out at 0.85
        score = clamp01(0.95 if successes > 0 else base)
        return (
            Alert(
                detector=self.name,
                category=ThreatCategory.CREDENTIAL_STUFFING,
                severity=Severity.for_score(score),
                score=score,
                subject=ip,
                source_ip=ip,
                ts=event.ts,
                title=f"Credential-stuffing pattern from {ip}",
                description=(
                    f"{distinct} distinct usernames in {cfg.window:.0f}s "
                    f"(failure ratio {failure_ratio:.0%}, {successes} success(es))."
                ),
                evidence=make_evidence(
                    distinct_usernames=distinct,
                    attempts=attempts,
                    failures=failures,
                    successes=successes,
                    failure_ratio=round(failure_ratio, 3),
                ),
                recommended_action="block_ip" if successes else "challenge",
                dedup_key=f"{self.name}:{ip}",
            ),
        )

    def sweep(self, now_mono: float) -> None:
        ttl = self.config.state_ttl
        stale = [ip for ip, st in self._state.items() if now_mono - st.last_mono > ttl]
        for ip in stale:
            del self._state[ip]


__all__ = ["CredentialStuffingConfig", "CredentialStuffingDetector"]
