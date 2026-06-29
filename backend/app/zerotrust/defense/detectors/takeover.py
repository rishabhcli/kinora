"""Account-takeover (ATO) detection.

Takeover is recognised by *a successful auth that breaks the account's own
history*: a login from an ip/device/geo the account has never used, especially
right after failed attempts (the cracked-it moment), or two successes too far
apart in space to be the same human ("impossible travel"). Unlike the volume
detectors this one is **per-account** and learns each account's normal
fingerprint set.

Per principal it tracks a bounded recent history of:

* source ips and user-agent fingerprints seen on *successful* logins; and
* the (lat, lon, time) of the last success, when geo is supplied in ``meta``.

A success from an entirely new ip *and* new device raises a medium signal;
adding impossible travel or a preceding failure burst raises it to high/critical.
A brand-new account (empty history) is never flagged — its first logins *define*
the baseline.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass, field

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
from ..windows import SlidingCounter
from .base import DetectorBase

_EARTH_KM = 6371.0


def _haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1 = math.radians(a[0]), math.radians(a[1])
    lat2, lon2 = math.radians(b[0]), math.radians(b[1])
    dlat, dlon = lat2 - lat1, lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * _EARTH_KM * math.asin(min(1.0, math.sqrt(h)))


@dataclass(slots=True)
class TakeoverConfig:
    """Tuning for :class:`AccountTakeoverDetector`."""

    history: int = 10
    """How many recent successful fingerprints to remember per account."""
    recent_failure_window: float = 600.0
    """Window (seconds) over which preceding failures escalate a new-device login."""
    recent_failure_escalation: int = 5
    """Failures within the window that push a new-device success to high severity."""
    max_travel_kmh: float = 900.0
    """Plausible travel speed; exceeding it between two successes is "impossible"."""
    state_ttl: float = 30 * 24 * 3600.0
    """Accounts idle longer than this are forgotten (keeps the map bounded)."""

    def __post_init__(self) -> None:
        if self.history < 1:
            raise ValueError("history must be >= 1")


@dataclass(slots=True)
class _AcctState:
    ips: list[str] = field(default_factory=list)
    fingerprints: list[str] = field(default_factory=list)
    last_geo: tuple[float, float] | None = None
    last_geo_ts: float = 0.0
    failures: SlidingCounter | None = None
    last_mono: float = 0.0
    seen_success: bool = False


class AccountTakeoverDetector(DetectorBase):
    """Per-account ATO detector: new device/ip/geo on a successful login."""

    name = "account_takeover"
    kinds = frozenset({EventKind.AUTH})

    def __init__(
        self,
        *,
        name: str = "account_takeover",
        config: TakeoverConfig | None = None,
        clock: Clock | None = None,
    ) -> None:
        super().__init__(clock=clock)
        self.name = name
        self.config = config or TakeoverConfig()
        self._state: dict[str, _AcctState] = {}

    @staticmethod
    def _fingerprint(event: SecurityEvent) -> str:
        return event.user_agent or "?"

    def _state_for(self, principal: str) -> _AcctState:
        st = self._state.get(principal)
        if st is None:
            st = _AcctState(failures=SlidingCounter(self.config.recent_failure_window))
            self._state[principal] = st
        return st

    def _remember(self, st: _AcctState, ip: str, fp: str) -> None:
        if ip not in st.ips:
            st.ips.append(ip)
            del st.ips[: -self.config.history]
        if fp not in st.fingerprints:
            st.fingerprints.append(fp)
            del st.fingerprints[: -self.config.history]

    def _observe(self, event: SecurityEvent) -> Iterable[Alert]:
        principal = event.principal
        if principal is None:
            return ()  # ATO is an *account* signal; anonymous failures go elsewhere
        now = self.clock.mono()
        st = self._state_for(principal)
        st.last_mono = now
        assert st.failures is not None

        if event.outcome is AuthOutcome.FAILURE:
            st.failures.hit(now)
            return ()
        if event.outcome is not AuthOutcome.SUCCESS:
            return ()

        ip = event.source_ip
        fp = self._fingerprint(event)
        new_ip = bool(st.ips) and ip not in st.ips
        new_device = bool(st.fingerprints) and fp not in st.fingerprints
        first_ever = not st.seen_success

        # Impossible travel against the last successful geo.
        impossible_travel = False
        speed_kmh = 0.0
        geo = event.get("geo_lat"), event.get("geo_lon")
        cur_geo: tuple[float, float] | None = None
        if geo[0] is not None and geo[1] is not None:
            cur_geo = (float(geo[0]), float(geo[1]))
            if st.last_geo is not None:
                dt_h = max(1e-6, (event.ts - st.last_geo_ts) / 3600.0)
                dist = _haversine_km(st.last_geo, cur_geo)
                speed_kmh = dist / dt_h
                impossible_travel = speed_kmh > self.config.max_travel_kmh

        recent_failures = st.failures.count(now)

        alerts: list[Alert] = []
        if not first_ever and (new_ip or new_device or impossible_travel):
            score = 0.5
            if new_ip and new_device:
                score = 0.7
            if recent_failures >= self.config.recent_failure_escalation:
                score = max(score, 0.85)
            if impossible_travel:
                score = max(score, 0.95)
            score = clamp01(score)
            reasons = [
                r
                for r, on in (
                    ("new_ip", new_ip),
                    ("new_device", new_device),
                    ("impossible_travel", impossible_travel),
                    ("after_failures", recent_failures >= self.config.recent_failure_escalation),
                )
                if on
            ]
            alerts.append(
                Alert(
                    detector=self.name,
                    category=ThreatCategory.ACCOUNT_TAKEOVER,
                    severity=Severity.for_score(score),
                    score=score,
                    subject=principal,
                    source_ip=ip,
                    ts=event.ts,
                    title=f"Possible account takeover of {principal}",
                    description=(
                        f"Successful login from a new context ({', '.join(reasons)}) "
                        f"for an account with established history."
                    ),
                    evidence=make_evidence(
                        new_ip=new_ip,
                        new_device=new_device,
                        impossible_travel=impossible_travel,
                        travel_speed_kmh=round(speed_kmh, 1),
                        recent_failures=recent_failures,
                        reasons=tuple(reasons),
                    ),
                    recommended_action="revoke_sessions_and_step_up",
                    dedup_key=f"{self.name}:{principal}",
                )
            )

        # Update the account's baseline with this successful context.
        st.seen_success = True
        self._remember(st, ip, fp)
        if cur_geo is not None:
            st.last_geo = cur_geo
            st.last_geo_ts = event.ts
        return alerts

    def sweep(self, now_mono: float) -> None:
        ttl = self.config.state_ttl
        stale = [p for p, st in self._state.items() if now_mono - st.last_mono > ttl]
        for p in stale:
            del self._state[p]


__all__ = ["AccountTakeoverDetector", "TakeoverConfig"]
