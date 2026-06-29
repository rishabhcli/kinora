"""Behavioural-anomaly detection via isolation-forest-lite.

Where the rule/rate detectors encode *known* attack shapes, this one is the
catch-all for *unknown* ones: it summarises each subject's behaviour over a
window into a small feature vector and flags vectors that don't look like
anything in a learned reference set. It is the unsupervised safety net.

Operation is two-phase, which keeps it deterministic and offline-trainable:

1. **fit** — given a corpus of reference feature vectors (built from benign
   traffic), train the forest once;
2. **score** — for each subject, accumulate features over a window and, once the
   window has enough events, score the current vector; an outlier raises an alert.

The default feature extractor produces a generic 6-tuple from windowed counters
(request rate, distinct targets, failure ratio, distinct user-agents, error
ratio, burst ratio). A caller can inject a domain-specific extractor without
touching the forest. The detector never fits implicitly — an unfitted detector
simply emits nothing, so it is safe to register before training data exists.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass

from ..clock import Clock
from ..stats import IsolationForestLite
from ..types import Alert, EventKind, SecurityEvent, Severity, ThreatCategory, make_evidence
from ..windows import DistinctWindow, SlidingCounter
from .base import DetectorBase


@dataclass(slots=True)
class _SubjectFeatures:
    requests: SlidingCounter
    errors: SlidingCounter
    failures: SlidingCounter
    targets: DistinctWindow
    agents: DistinctWindow
    last_mono: float = 0.0
    seen: int = 0


@dataclass(slots=True)
class BehavioralConfig:
    """Tuning for :class:`BehavioralDetector`."""

    window: float = 120.0
    min_events: int = 15
    """Events accumulated for a subject before its vector is scored."""
    score_threshold: float = 0.62
    """Isolation-forest anomaly score above which an alert fires."""
    n_trees: int = 64
    sample_size: int = 256
    seed: int = 1337
    distinct_cap: int = 4096
    state_ttl: float = 3600.0

    def __post_init__(self) -> None:
        if self.window <= 0:
            raise ValueError("window must be positive")
        if self.min_events < 1:
            raise ValueError("min_events must be >= 1")


def _is_error(event: SecurityEvent) -> bool:
    return event.status_code is not None and event.status_code >= 400


class BehavioralDetector(DetectorBase):
    """Isolation-forest-lite over per-subject windowed feature vectors."""

    name = "behavioral_anomaly"
    kinds = frozenset({EventKind.AUTH, EventKind.ACCESS, EventKind.HTTP, EventKind.AUDIT})

    def __init__(
        self,
        *,
        name: str = "behavioral_anomaly",
        config: BehavioralConfig | None = None,
        feature_extractor: (
            Callable[[BehavioralDetector, str, SecurityEvent], list[float]] | None
        ) = None,
        clock: Clock | None = None,
    ) -> None:
        super().__init__(clock=clock)
        self.name = name
        self.config = config or BehavioralConfig()
        self._forest = IsolationForestLite(
            n_trees=self.config.n_trees,
            sample_size=self.config.sample_size,
            seed=self.config.seed,
        )
        self._state: dict[str, _SubjectFeatures] = {}
        self._extract = feature_extractor or BehavioralDetector._default_features

    @property
    def fitted(self) -> bool:
        return self._forest.fitted

    def fit(self, reference_vectors: Sequence[Sequence[float]]) -> BehavioralDetector:
        """Train the forest on a corpus of benign feature vectors."""
        self._forest.fit(reference_vectors)
        return self

    def _state_for(self, subject: str) -> _SubjectFeatures:
        st = self._state.get(subject)
        if st is None:
            w = self.config.window
            cap = self.config.distinct_cap
            st = _SubjectFeatures(
                requests=SlidingCounter(w),
                errors=SlidingCounter(w),
                failures=SlidingCounter(w),
                targets=DistinctWindow(w, cap=cap),
                agents=DistinctWindow(w, cap=cap),
            )
            self._state[subject] = st
        return st

    def _default_features(self, subject: str, event: SecurityEvent) -> list[float]:
        st = self._state_for(subject)
        now = self.clock.mono()
        st.last_mono = now
        st.seen += 1
        requests = st.requests.hit(now)
        if _is_error(event):
            st.errors.hit(now)
        if event.outcome is not None and str(event.outcome) == "failure":
            st.failures.hit(now)
        distinct_targets = st.targets.add(event.target or "?", now)
        distinct_agents = st.agents.add(event.user_agent or "?", now)
        errors = st.errors.count(now)
        failures = st.failures.count(now)
        rate = requests / self.config.window
        return [
            rate,
            float(distinct_targets),
            failures / requests if requests else 0.0,
            float(distinct_agents),
            errors / requests if requests else 0.0,
            distinct_targets / requests if requests else 0.0,
        ]

    def _observe(self, event: SecurityEvent) -> Iterable[Alert]:
        subject = event.subject
        vector = self._extract(self, subject, event)
        st = self._state[subject]
        if not self._forest.fitted or st.seen < self.config.min_events:
            return ()
        score = self._forest.score(vector)
        if score < self.config.score_threshold:
            return ()
        return (
            Alert(
                detector=self.name,
                category=ThreatCategory.BEHAVIORAL_ANOMALY,
                severity=Severity.for_score(score),
                score=score,
                subject=subject,
                source_ip=event.source_ip,
                ts=event.ts,
                title=f"Anomalous behaviour profile for {subject}",
                description=(
                    "The subject's windowed behaviour vector is an outlier against "
                    "the learned benign reference set."
                ),
                evidence=make_evidence(
                    feature_vector=tuple(round(x, 4) for x in vector),
                    isolation_score=round(score, 4),
                ),
                recommended_action="review",
                dedup_key=f"{self.name}:{subject}",
            ),
        )

    def sweep(self, now_mono: float) -> None:
        ttl = self.config.state_ttl
        stale = [s for s, st in self._state.items() if now_mono - st.last_mono > ttl]
        for s in stale:
            del self._state[s]


__all__ = ["BehavioralConfig", "BehavioralDetector"]
