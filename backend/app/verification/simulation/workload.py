"""Reader workloads — the demand signal that drives the whole loop (kinora.md §4.3
reading-position model, §4.7 dwell/idle, §4.8 seek, §4.11 failure-mode archetypes).

The simulation is *demand-pulled by attention* (kinora.md §9.8): nothing renders
until a reader scrolls. This module is the source of that attention — a seeded,
deterministic stream of reader intents (advance at a velocity, dwell, idle, seek)
that the :mod:`~app.verification.simulation.system` wiring turns into scheduler
ticks on the virtual clock.

These are deliberately distinct from ``app.scheduler.simulation.ReaderProfile``:
those generate a *fixed script* for the zero-spend buffer-trace harness. Here a
:class:`ReaderModel` is *stateful and stochastic* — it makes each next decision
from the run's :class:`~app.verification.simulation.core.Prng`, so a single
archetype expands into thousands of distinct seeded sessions for the sweep, each
exercising a different sequence of advances, pauses, and jumps. The archetypes map
to the §4.11 failure-mode table (steady, skimmer, thinker, seeker, erratic), which
is exactly the population a resilience sweep must cover.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from app.verification.simulation.core import Prng


class IntentKind(StrEnum):
    """The reader intents the system reacts to (a settled §4.7 position update)."""

    ADVANCE = "advance"  # focus word moves forward at a velocity
    DWELL = "dwell"  # hold position briefly (sub-idle)
    IDLE = "idle"  # hold long enough to trip the §4.7 idle-pause
    SEEK = "seek"  # jump to a far word (§4.8)


@dataclass(frozen=True, slots=True)
class ReaderIntent:
    """One settled reader intent at a virtual instant.

    ``words`` is the forward delta since the last intent; ``velocity_wps`` the
    instantaneous (unclamped) read velocity that produced it; ``target_word`` set
    only for a ``SEEK``. The system folds these into the real
    :class:`~app.scheduler.model.SchedulerSession` exactly as a debounced client
    update would.
    """

    kind: IntentKind
    dt_ms: int
    words: int = 0
    velocity_wps: float = 4.0
    target_word: int | None = None


@dataclass(slots=True)
class ReaderModel:
    """A stateful, seeded reader that emits one intent per :meth:`next_intent`.

    The model holds the current focus word and a base velocity, and each step
    rolls the PRNG to decide what the reader does next, biased by the archetype.
    It is the live, stochastic counterpart to the fixed ``ReaderProfile`` traces —
    the same archetype with a different seed produces a genuinely different
    session, which is what a sweep needs to find ordering bugs that only a specific
    advance/seek interleaving exposes.
    """

    prng: Prng
    archetype: str = "steady"
    focus_word: int = 0
    base_wps: float = 4.0
    book_words: int = 6000
    #: Cadence of settled intents — the §4.7 scroll-settle window (ms).
    settle_ms: int = 2_500

    def next_intent(self) -> ReaderIntent:
        """Emit the reader's next settled intent, advancing internal state."""
        roll = self.prng.random()

        if self.archetype == "steady":
            return self._advance(self.base_wps)

        if self.archetype == "variable":
            # Jitter velocity ±50% each step.
            v = max(0.5, self.prng.jitter(self.base_wps, 0.5))
            return self._advance(v)

        if self.archetype == "skimmer":
            # Mostly skim fast (above the 12 wps clamp); occasional normal read.
            v = self.prng.uniform(13.0, 24.0) if roll < 0.8 else self.base_wps
            return self._advance(v)

        if self.archetype == "thinker":
            # Read, then frequently pause long enough to idle.
            if roll < 0.35:
                return ReaderIntent(IntentKind.IDLE, dt_ms=self.prng.randint(9_000, 30_000))
            if roll < 0.5:
                return ReaderIntent(IntentKind.DWELL, dt_ms=self.prng.randint(2_000, 6_000))
            return self._advance(self.prng.uniform(2.0, 4.0))

        if self.archetype == "seeker":
            # Read with periodic far jumps (forward or backward).
            if roll < 0.2:
                target = self.prng.randint(0, max(1, self.book_words - 1))
                self.focus_word = target
                return ReaderIntent(IntentKind.SEEK, dt_ms=self.settle_ms, target_word=target)
            return self._advance(self.base_wps)

        # "erratic" — every behaviour, maximally adversarial interleaving.
        if roll < 0.5:
            v = self.prng.uniform(0.8, 24.0)
            return self._advance(v)
        if roll < 0.65:
            target = self.prng.randint(0, max(1, self.book_words - 1))
            self.focus_word = target
            return ReaderIntent(IntentKind.SEEK, dt_ms=self.settle_ms, target_word=target)
        if roll < 0.8:
            return ReaderIntent(IntentKind.IDLE, dt_ms=self.prng.randint(9_000, 40_000))
        return ReaderIntent(IntentKind.DWELL, dt_ms=self.prng.randint(1_000, 5_000))

    def _advance(self, velocity_wps: float) -> ReaderIntent:
        words = max(0, int(round(velocity_wps * (self.settle_ms / 1000.0))))
        self.focus_word += words
        return ReaderIntent(
            IntentKind.ADVANCE,
            dt_ms=self.settle_ms,
            words=words,
            velocity_wps=velocity_wps,
        )


#: The §4.11 failure-mode archetypes the sweep must cover.
ARCHETYPES: tuple[str, ...] = (
    "steady",
    "variable",
    "skimmer",
    "thinker",
    "seeker",
    "erratic",
)


def make_reader(
    prng: Prng, archetype: str, *, book_words: int, base_wps: float = 4.0
) -> ReaderModel:
    """Construct a seeded :class:`ReaderModel` for ``archetype``.

    The archetype is validated against :data:`ARCHETYPES` so a typo surfaces as an
    error rather than silently degrading to the erratic default.
    """
    if archetype not in ARCHETYPES:
        raise ValueError(f"unknown reader archetype {archetype!r}; expected one of {ARCHETYPES}")
    return ReaderModel(prng=prng, archetype=archetype, book_words=book_words, base_wps=base_wps)


__all__ = [
    "ARCHETYPES",
    "IntentKind",
    "ReaderIntent",
    "ReaderModel",
    "make_reader",
]
