"""Sequence-anomaly detection via online Markov surprise.

Some attacks aren't about *volume* but *order*: a request pattern that no normal
session ever produces (export-everything before any read; password-reset →
email-change → payout in seconds; an admin action with no preceding read). A
first-order Markov model over a subject's action sequence captures "what usually
follows what"; an observed transition whose learned probability is very low is
*surprising*, and a run of surprising transitions is an alert.

The model is online and per-subject:

* a global transition table learns ``P(next | prev)`` across all subjects
  (so a brand-new user inherits population norms, not a cold blank slate); and
* each subject carries only its previous action + a decaying surprise
  accumulator, so memory is O(actions) globally and O(1) per subject.

Surprise is ``-log2 P(next|prev)`` with Laplace smoothing; the per-event score is
a bounded function of the smoothed surprise, and an alert fires when the decaying
accumulator crosses a threshold (a *sustained* oddity, not one rare-but-benign
click).
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from ..clock import Clock
from ..stats import clamp01
from ..types import Alert, EventKind, SecurityEvent, Severity, ThreatCategory, make_evidence
from .base import DetectorBase

_START = "\x02start"


def _action_of(event: SecurityEvent) -> str:
    """The token an event contributes to the sequence."""
    if event.action:
        return event.action
    if event.kind is EventKind.AUTH and event.outcome is not None:
        return f"auth.{event.outcome}"
    return f"{event.kind}.{event.status_code or 0}"


@dataclass(slots=True)
class SequenceConfig:
    """Tuning for :class:`SequenceAnomalyDetector`."""

    surprise_threshold: float = 8.0
    """Accumulated surprise (bits) above which an alert fires."""
    decay_half_life: float = 30.0
    """Half-life (seconds) of the per-subject surprise accumulator."""
    laplace: float = 1.0
    """Additive-smoothing constant for unseen transitions."""
    min_prior: int = 20
    """Global transitions observed before scoring is trusted (warm-up)."""
    state_ttl: float = 3600.0
    vocab_cap: int = 512
    """Max distinct actions tracked globally (bounds the table)."""


@dataclass(slots=True)
class _SubjectState:
    prev: str = _START
    surprise: float = 0.0
    last_mono: float = 0.0


class SequenceAnomalyDetector(DetectorBase):
    """Per-subject Markov-surprise detector over action sequences."""

    name = "sequence_anomaly"
    kinds = frozenset({EventKind.AUTH, EventKind.ACCESS, EventKind.AUDIT})

    def __init__(
        self,
        *,
        name: str = "sequence_anomaly",
        config: SequenceConfig | None = None,
        action_of: Callable[[SecurityEvent], str] = _action_of,
        clock: Clock | None = None,
    ) -> None:
        super().__init__(clock=clock)
        self.name = name
        self.config = config or SequenceConfig()
        self._action_of = action_of
        # transitions[prev][next] = count
        self._trans: dict[str, dict[str, int]] = {}
        self._row_totals: dict[str, int] = {}
        self._vocab: set[str] = set()
        self._total = 0
        self._subjects: dict[str, _SubjectState] = {}

    # -- model ------------------------------------------------------------- #
    def _learn(self, prev: str, nxt: str) -> None:
        if len(self._vocab) >= self.config.vocab_cap and nxt not in self._vocab:
            return  # vocab is full; ignore novel tokens rather than grow forever
        self._vocab.add(prev)
        self._vocab.add(nxt)
        row = self._trans.setdefault(prev, {})
        row[nxt] = row.get(nxt, 0) + 1
        self._row_totals[prev] = self._row_totals.get(prev, 0) + 1
        self._total += 1

    def _prob(self, prev: str, nxt: str) -> float:
        lap = self.config.laplace
        vocab = max(1, len(self._vocab))
        row = self._trans.get(prev, {})
        num = row.get(nxt, 0) + lap
        den = self._row_totals.get(prev, 0) + lap * vocab
        return num / den

    def _surprise_bits(self, prev: str, nxt: str) -> float:
        return -math.log2(max(1e-12, self._prob(prev, nxt)))

    # -- detector ---------------------------------------------------------- #
    def _observe(self, event: SecurityEvent) -> Iterable[Alert]:
        subj = event.subject
        nxt = self._action_of(event)
        now = self.clock.mono()
        st = self._subjects.get(subj)
        if st is None:
            st = _SubjectState(last_mono=now)
            self._subjects[subj] = st

        # Decay the accumulator toward 0 over time.
        if st.surprise > 0.0:
            dt = max(0.0, now - st.last_mono)
            st.surprise *= math.exp(-math.log(2.0) / self.config.decay_half_life * dt)

        bits = self._surprise_bits(st.prev, nxt)
        warm = self._total >= self.config.min_prior
        if warm:
            st.surprise += bits

        alerts: list[Alert] = []
        if warm and st.surprise >= self.config.surprise_threshold:
            score = clamp01(st.surprise / (self.config.surprise_threshold * 2.0))
            alerts.append(
                Alert(
                    detector=self.name,
                    category=ThreatCategory.SEQUENCE_ANOMALY,
                    severity=Severity.for_score(score),
                    score=score,
                    subject=subj,
                    source_ip=event.source_ip,
                    ts=event.ts,
                    title=f"Unusual action sequence for {subj}",
                    description=(
                        f"Transition {st.prev!r}->{nxt!r} ({bits:.1f} bits) drove the "
                        f"sustained-surprise accumulator to {st.surprise:.1f} bits."
                    ),
                    evidence=make_evidence(
                        prev=st.prev,
                        next=nxt,
                        transition_bits=round(bits, 3),
                        accumulated_bits=round(st.surprise, 3),
                    ),
                    recommended_action="step_up_auth",
                    dedup_key=f"{self.name}:{subj}",
                )
            )
            st.surprise = 0.0  # reset after firing so we don't spam

        # Learn the transition *after* scoring it (so a fresh oddity scores high).
        self._learn(st.prev, nxt)
        st.prev = nxt
        st.last_mono = now
        return alerts

    def sweep(self, now_mono: float) -> None:
        ttl = self.config.state_ttl
        stale = [s for s, st in self._subjects.items() if now_mono - st.last_mono > ttl]
        for s in stale:
            del self._subjects[s]


__all__ = ["SequenceAnomalyDetector", "SequenceConfig"]
