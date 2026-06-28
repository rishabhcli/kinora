"""The reader as a state machine — realistic generation-on-scroll traffic (§4.3/§4.7/§4.8).

A real Kinora client does not issue requests uniformly. It tracks a focus word
``w`` and a velocity ``v`` (§4.3), debounces scroll into a settled intent every
~200ms (§4.7), occasionally **seeks** to a far page (§4.8), **skims** at a high
velocity, and goes **idle** when the reader puts the book down (§4.7). The shape
of that traffic — bursty intent updates, the odd seek, long idle gaps — is what a
load test must reproduce to stress the Scheduler the way production does.

:class:`ReaderModel` turns a reader *persona* + a seeded RNG + an injected clock
into a stream of :class:`ReaderAction` records (``intent`` / ``seek`` / ``idle``)
that the load runner maps onto ``POST /sessions/{id}/intent`` and
``POST /sessions/{id}/seek``. It is fully deterministic given its seed, so the
unit tests pin the emitted action sequence, the velocity clamping, and the state
transitions exactly — no wall-clock, no network.
"""

from __future__ import annotations

import random
from collections.abc import Iterator
from dataclasses import dataclass, field
from enum import StrEnum

from app.scheduler.zones import (
    DEFAULT_VELOCITY_WPS,
    VELOCITY_CLAMP_HIGH,
    clamp_velocity,
)

#: The §4.7 scroll-settle debounce — intent updates fire no faster than this.
SETTLE_INTERVAL_S = 0.2
#: The §4.7 idle-pause threshold — no activity for this long => idle.
IDLE_PAUSE_S = 8.0


class ReaderState(StrEnum):
    """The reader's current behavioural mode."""

    READING = "reading"  # steady forward dwell-and-advance
    SKIMMING = "skimming"  # velocity above the clamp ceiling (§4.6 unstable)
    SEEKING = "seeking"  # just jumped to a far position (§4.8)
    IDLE = "idle"  # put the book down (§4.7 idle-pause)


class ActionKind(StrEnum):
    """The wire action a reader step maps to."""

    INTENT = "intent"  # POST /sessions/{id}/intent
    SEEK = "seek"  # POST /sessions/{id}/seek
    IDLE = "idle"  # no request issued this step (reader is paused)


@dataclass(frozen=True, slots=True)
class ReaderAction:
    """One step of reader behaviour the runner turns into a request (or a pause).

    ``t_s`` is the model-clock timestamp of the action; ``focus_word`` and
    ``velocity_wps`` are the §4.3 intent payload (velocity already clamped to the
    [0.5×, 3×] band the backend uses); ``raw_velocity_wps`` is the pre-clamp
    estimate that drives skim detection (§4.6).
    """

    kind: ActionKind
    t_s: float
    state: ReaderState
    focus_word: int
    velocity_wps: float
    raw_velocity_wps: float
    #: Set only for a ``SEEK`` — the target word the reader jumped to.
    seek_word: int | None = None


@dataclass(frozen=True, slots=True)
class ReaderPersona:
    """A parameterised reader behaviour (the knobs a scenario tunes).

    All probabilities are *per settle step* (every :data:`SETTLE_INTERVAL_S`).
    Defaults describe an engaged reader of a ~25-minute book: ~240 wpm base,
    rare seeks, occasional skim bursts, the odd think-pause.
    """

    name: str = "engaged"
    #: Mean steady reading velocity (words/sec) before per-step jitter.
    base_velocity_wps: float = DEFAULT_VELOCITY_WPS
    #: Multiplicative log-normal jitter sigma applied to velocity each step.
    velocity_jitter: float = 0.18
    #: Per-step probability of starting a skim burst (raw velocity > clamp).
    p_skim: float = 0.02
    #: Per-step probability of a far seek (§4.8).
    p_seek: float = 0.01
    #: Per-step probability of pausing (entering idle / think-time).
    p_pause: float = 0.015
    #: Multiplier on base velocity while skimming (raw, pre-clamp).
    skim_velocity_mult: float = 4.0
    #: Mean think-pause length in seconds (idle dwell before resuming).
    mean_pause_s: float = 6.0
    #: How far ahead/behind a seek lands, in words (uniform +/- this).
    seek_span_words: int = 4000
    #: Length of the book in words (the reader stops/loops at the end).
    book_words: int = 60_000


@dataclass
class ReaderModel:
    """A deterministic generator of one reader's request stream (§4.3/§4.7/§4.8).

    Construct with a persona, a start word, and a seed; then iterate
    :meth:`steps` for a fixed model-time budget. The model owns its own clock
    (advanced by :data:`SETTLE_INTERVAL_S` per step, plus pause dwell), so it
    needs no wall-clock and is reproducible bit-for-bit.
    """

    persona: ReaderPersona = field(default_factory=ReaderPersona)
    start_word: int = 0
    seed: int = 0
    #: Internal mutable state.
    _rng: random.Random = field(init=False, repr=False)
    _w: int = field(init=False, repr=False)
    _t: float = field(init=False, repr=False)
    _state: ReaderState = field(init=False, repr=False)
    _skim_remaining: int = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._rng = random.Random(self.seed)
        self._w = max(0, self.start_word)
        self._t = 0.0
        self._state = ReaderState.READING
        self._skim_remaining = 0

    # -- single step --------------------------------------------------------- #

    def _sample_velocity(self, base: float) -> float:
        """Apply per-step log-normal jitter to a base velocity (>= a small floor)."""
        if self.persona.velocity_jitter <= 0.0:
            return base
        factor = self._rng.lognormvariate(0.0, self.persona.velocity_jitter)
        return max(0.1, base * factor)

    def _advance(self, velocity_wps: float, dt_s: float) -> None:
        """Move the focus word forward by ``v * dt``, wrapping at the book end."""
        self._w += int(velocity_wps * dt_s)
        if self._w >= self.persona.book_words:
            # Reached the end: loop back to the start (a re-read; cache-friendly).
            self._w = self._w % max(1, self.persona.book_words)

    def step(self) -> ReaderAction:
        """Advance the model by one settle interval and return the action taken."""
        p = self.persona
        self._t += SETTLE_INTERVAL_S

        # 1. Resolve an active idle pause first (think-time): no request issued.
        if self._state is ReaderState.IDLE:
            # Pause ends probabilistically per step (geometric dwell ~ mean_pause_s).
            end_prob = SETTLE_INTERVAL_S / max(SETTLE_INTERVAL_S, p.mean_pause_s)
            if self._rng.random() < end_prob:
                self._state = ReaderState.READING
            else:
                return ReaderAction(
                    kind=ActionKind.IDLE,
                    t_s=self._t,
                    state=ReaderState.IDLE,
                    focus_word=self._w,
                    velocity_wps=0.0,
                    raw_velocity_wps=0.0,
                )

        # 2. A far seek pre-empts steady reading (§4.8).
        if self._rng.random() < p.p_seek:
            delta = self._rng.randint(-p.seek_span_words, p.seek_span_words)
            target = min(max(0, self._w + delta), max(0, p.book_words - 1))
            self._w = target
            self._state = ReaderState.SEEKING
            # After a seek, velocity resets to default until fresh samples arrive (§4.8).
            v = clamp_velocity(p.base_velocity_wps)
            return ReaderAction(
                kind=ActionKind.SEEK,
                t_s=self._t,
                state=ReaderState.SEEKING,
                focus_word=target,
                velocity_wps=v,
                raw_velocity_wps=p.base_velocity_wps,
                seek_word=target,
            )

        # 3. Maybe enter / continue a skim burst (raw velocity above the clamp).
        if self._skim_remaining > 0 or self._rng.random() < p.p_skim:
            if self._skim_remaining <= 0:
                self._skim_remaining = self._rng.randint(3, 10)
            self._skim_remaining -= 1
            self._state = ReaderState.SKIMMING
            raw = self._sample_velocity(p.base_velocity_wps * p.skim_velocity_mult)
            clamped = clamp_velocity(raw)
            self._advance(clamped, SETTLE_INTERVAL_S)
            return ReaderAction(
                kind=ActionKind.INTENT,
                t_s=self._t,
                state=ReaderState.SKIMMING,
                focus_word=self._w,
                velocity_wps=clamped,
                raw_velocity_wps=raw,
            )

        # 4. Maybe pause (enter idle) instead of advancing.
        if self._rng.random() < p.p_pause:
            self._state = ReaderState.IDLE
            return ReaderAction(
                kind=ActionKind.IDLE,
                t_s=self._t,
                state=ReaderState.IDLE,
                focus_word=self._w,
                velocity_wps=0.0,
                raw_velocity_wps=0.0,
            )

        # 5. Steady forward reading (the common case).
        self._state = ReaderState.READING
        raw = self._sample_velocity(p.base_velocity_wps)
        clamped = clamp_velocity(raw)
        self._advance(clamped, SETTLE_INTERVAL_S)
        return ReaderAction(
            kind=ActionKind.INTENT,
            t_s=self._t,
            state=ReaderState.READING,
            focus_word=self._w,
            velocity_wps=clamped,
            raw_velocity_wps=raw,
        )

    # -- bounded stream ------------------------------------------------------ #

    def steps(self, *, duration_s: float) -> Iterator[ReaderAction]:
        """Yield reader actions until the model clock reaches ``duration_s``.

        The stream includes ``IDLE`` actions (no request) so the runner can honour
        think-time without issuing traffic — exactly the §4.7 "idle reader
        generates nothing" behaviour, observable as gaps in the request stream.
        """
        while self._t < duration_s:
            yield self.step()

    @property
    def state(self) -> ReaderState:
        """The reader's current behavioural state."""
        return self._state

    @property
    def focus_word(self) -> int:
        """The reader's current focus word ``w``."""
        return self._w

    @property
    def clock_s(self) -> float:
        """The model-clock time elapsed (seconds)."""
        return self._t


def is_skim_velocity(raw_velocity_wps: float) -> bool:
    """Whether a raw velocity reads as a skim (above the §4.6 clamp ceiling)."""
    return abs(raw_velocity_wps) > VELOCITY_CLAMP_HIGH


__all__ = [
    "IDLE_PAUSE_S",
    "SETTLE_INTERVAL_S",
    "ActionKind",
    "ReaderAction",
    "ReaderModel",
    "ReaderPersona",
    "ReaderState",
    "is_skim_velocity",
]
