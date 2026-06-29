"""Rate-based anomaly detection.

The simplest, highest-precision threat signal: *too many of a thing, too fast*.
But a fixed threshold is wrong — a popular book legitimately gets more traffic
than a quiet one. So instead of a constant ceiling we learn a per-key baseline
with an EWMA/MAD :class:`~app.zerotrust.defense.stats.RobustScaler` and alert on
the *robust departure* from it. A hard absolute floor still catches a cold-start
burst before the baseline has data.

Keyed by a configurable extractor (default: source ip), this one detector backs
brute-force, request-flood and burst signals. More specialised detectors
(credential-stuffing, scraping) layer richer keys on top of the same machinery.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass

from ..clock import Clock
from ..stats import RobustScaler
from ..types import Alert, EventKind, SecurityEvent, Severity, ThreatCategory, make_evidence
from ..windows import SlidingCounter
from .base import DetectorBase


def _key_source_ip(event: SecurityEvent) -> str:
    return event.source_ip


def _key_subject(event: SecurityEvent) -> str:
    return event.subject


@dataclass(slots=True)
class RateConfig:
    """Tuning for :class:`RateAnomalyDetector`."""

    window: float = 60.0
    """Trailing window (seconds) the rate is counted over."""
    baseline_half_life: float = 900.0
    """EWMA half-life (seconds) for the learned per-key baseline."""
    absolute_floor: int = 0
    """Hard count within the window above which an alert always fires.

    ``0`` disables the floor (anomaly-only). A sane brute-force floor is ~20.
    """
    min_observations: int = 8
    """Baseline observations required before the anomaly path can fire."""
    score_threshold: float = 0.55
    """Minimum anomaly score to emit an alert."""
    category: ThreatCategory = ThreatCategory.RATE_ANOMALY
    state_ttl: float = 3600.0
    """Idle seconds after which a key's state is swept away."""

    def __post_init__(self) -> None:
        if self.window <= 0:
            raise ValueError("window must be positive")
        if self.min_observations < 1:
            raise ValueError("min_observations must be >= 1")


@dataclass(slots=True)
class _KeyState:
    counter: SlidingCounter
    scaler: RobustScaler
    observations: int = 0
    last_mono: float = 0.0
    last_bucket_end: float = 0.0


class RateAnomalyDetector(DetectorBase):
    """Per-key windowed rate vs. an adaptive baseline.

    On each consumed event the key's in-window count is updated; once per window
    boundary the count is folded into the baseline so the scaler learns the
    key's normal cadence. An alert fires when the *current* count's robust
    departure from baseline crosses :attr:`RateConfig.score_threshold`, or
    immediately when an absolute floor is breached.
    """

    kinds = frozenset()  # all kinds; the key extractor decides relevance

    def __init__(
        self,
        *,
        name: str = "rate_anomaly",
        config: RateConfig | None = None,
        key: Callable[[SecurityEvent], str] = _key_source_ip,
        kinds: Iterable[EventKind] | None = None,
        clock: Clock | None = None,
    ) -> None:
        super().__init__(clock=clock)
        self.name = name
        self.config = config or RateConfig()
        self._key = key
        self.kinds = frozenset(kinds) if kinds else frozenset()
        self._state: dict[str, _KeyState] = {}

    def _state_for(self, key: str) -> _KeyState:
        st = self._state.get(key)
        if st is None:
            st = _KeyState(
                counter=SlidingCounter(self.config.window),
                scaler=RobustScaler(half_life=self.config.baseline_half_life),
            )
            self._state[key] = st
        return st

    def _observe(self, event: SecurityEvent) -> Iterable[Alert]:
        key = self._key(event)
        now = self.clock.mono()
        st = self._state_for(key)
        count = st.counter.hit(now)
        st.last_mono = now

        # Roll the baseline forward once per elapsed window so the scaler learns
        # a stable per-window rate rather than every transient count.
        if st.last_bucket_end == 0.0:
            st.last_bucket_end = now
        if now - st.last_bucket_end >= self.config.window:
            st.scaler.update(float(count), now)
            st.observations += 1
            st.last_bucket_end = now

        alerts: list[Alert] = []
        floor = self.config.absolute_floor
        if floor and count >= floor:
            alerts.append(self._alert(event, key, count, score=0.92, reason="absolute_floor"))
            return alerts

        if st.observations >= self.config.min_observations:
            score = st.scaler.score(float(count))
            if score >= self.config.score_threshold:
                alerts.append(self._alert(event, key, count, score=score, reason="baseline"))
        return alerts

    def _alert(
        self, event: SecurityEvent, key: str, count: int, *, score: float, reason: str
    ) -> Alert:
        cat = self.config.category
        return Alert(
            detector=self.name,
            category=cat,
            severity=Severity.for_score(score),
            score=score,
            subject=key,
            source_ip=event.source_ip,
            ts=event.ts,
            title=f"Anomalous request rate from {key}",
            description=(
                f"{count} events in {self.config.window:.0f}s exceeds the learned "
                f"baseline ({reason})."
            ),
            evidence=make_evidence(
                key=key,
                window_count=count,
                window_seconds=self.config.window,
                reason=reason,
                kind=str(event.kind),
            ),
            recommended_action="rate_limit",
            dedup_key=f"{self.name}:{key}",
        )

    def sweep(self, now_mono: float) -> None:
        ttl = self.config.state_ttl
        stale = [k for k, st in self._state.items() if now_mono - st.last_mono > ttl]
        for k in stale:
            del self._state[k]


__all__ = ["RateAnomalyDetector", "RateConfig"]
