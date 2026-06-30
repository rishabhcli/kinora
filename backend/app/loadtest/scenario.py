"""Reader-session journeys — load that mirrors how a human actually reads (§4).

A flat stream of identical requests does not exercise the Kinora data plane the
way real readers do. Real load is a sequence of *journeys*: a reader **opens a
book**, polls **buffer/session state** while they sit on a page, **turns pages**
on a slow human cadence (§4.1: ~60s/page, the buffer is measured in reading-time
ahead, not bytes), occasionally **jumps/seeks** to a new position (the §4.6
"skims/flips wildly" / trajectory-instability case), and eventually **closes**.

A :class:`Scenario` is a declarative description of that journey: an ordered list
of :class:`Step`\\ s, each naming a logical endpoint, a count, and the *think
time* a reader spends before the next step. The harness expands a scenario into
the per-virtual-user request sequence for the **closed-loop** model (a fixed
population of readers each looping a journey), and the per-step endpoint mix also
seeds the **open-loop** model (what fraction of arrivals hit each endpoint).

Think times are drawn from a distribution so a population of readers desynchronizes
naturally (otherwise every reader turns the page on the same tick — an artefact).
The draw uses an injected seeded RNG, so a scenario is fully reproducible.

Everything here is pure data + pure expansion; no I/O.
"""

from __future__ import annotations

import random
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum

from app.loadtest.target import LoadRequest

# --------------------------------------------------------------------------- #
# Canonical Kinora reading-plane endpoints (logical names, not URL paths).
# --------------------------------------------------------------------------- #


class ReadEndpoint(StrEnum):
    """The logical endpoints a reader session exercises."""

    OPEN_BOOK = "open_book"  # POST /sessions — open a book, start a session
    BUFFER_STATE = "buffer_state"  # GET buffer/session state (the §4.3 poll)
    PAGE_TURN = "page_turn"  # advance the focus playhead one page (§4.1)
    JUMP = "jump"  # seek to a new position (§4.6 cancel + re-promote)
    COMMENT = "comment"  # POST /sessions/{id}/comment (director note, §5.4)
    CLOSE = "close"  # end the session (idle-sweep / explicit close)


# --------------------------------------------------------------------------- #
# Think-time distributions
# --------------------------------------------------------------------------- #


class ThinkShape(StrEnum):
    """How a step's think time is drawn."""

    FIXED = "fixed"  # exactly ``mean_s``
    UNIFORM = "uniform"  # U[mean_s*(1-spread), mean_s*(1+spread)]
    EXPONENTIAL = "exponential"  # Exp with the given mean (heavy human pause)


@dataclass(frozen=True, slots=True)
class ThinkTime:
    """A think-time distribution between a step and the next request."""

    mean_s: float
    shape: ThinkShape = ThinkShape.EXPONENTIAL
    spread: float = 0.5  # UNIFORM half-width as a fraction of the mean

    def draw(self, rng: random.Random) -> float:
        if self.mean_s <= 0:
            return 0.0
        if self.shape is ThinkShape.FIXED:
            return self.mean_s
        if self.shape is ThinkShape.UNIFORM:
            lo = self.mean_s * (1.0 - self.spread)
            hi = self.mean_s * (1.0 + self.spread)
            return max(0.0, rng.uniform(lo, hi))
        return rng.expovariate(1.0 / self.mean_s)


@dataclass(frozen=True, slots=True)
class Step:
    """One leg of a reader journey: ``count`` calls to ``endpoint``, then think.

    ``count`` repeats the endpoint (e.g. "turn the page 8 times"); ``think``
    elapses *between* successive requests in the step and before the next step.
    ``payload`` is merged into each emitted :class:`LoadRequest`.
    """

    endpoint: str
    count: int = 1
    think: ThinkTime = field(default_factory=lambda: ThinkTime(0.0, ThinkShape.FIXED))
    payload: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.count < 1:
            raise ValueError("Step.count must be >= 1")


@dataclass(frozen=True, slots=True)
class Scenario:
    """A named reader-session journey as an ordered list of steps."""

    name: str
    steps: Sequence[Step]

    def __post_init__(self) -> None:
        if not self.steps:
            raise ValueError("Scenario must have at least one step")

    def expand(
        self, rng: random.Random, *, session_id: str | None = None
    ) -> Iterator[tuple[LoadRequest, float]]:
        """Yield ``(request, think_after_s)`` pairs for one run of the journey.

        ``think_after_s`` is the think time the closed-loop driver should sleep
        *after* sending this request (the human pause before the next request).
        ``session_id`` is woven into each payload so a virtual user's requests
        are attributable to one session in a real run.
        """
        for step in self.steps:
            for _ in range(step.count):
                payload: dict[str, object] = dict(step.payload)
                if session_id is not None:
                    payload.setdefault("session_id", session_id)
                yield (
                    LoadRequest(endpoint=step.endpoint, payload=payload),
                    step.think.draw(rng),
                )

    def endpoint_mix(self) -> dict[str, float]:
        """The fraction of requests hitting each endpoint over one journey.

        Used to *weight* open-loop arrivals so the offered mix matches the
        scenario even when arrivals are an undifferentiated Poisson stream.
        """
        counts: dict[str, int] = {}
        total = 0
        for step in self.steps:
            counts[step.endpoint] = counts.get(step.endpoint, 0) + step.count
            total += step.count
        return {ep: c / total for ep, c in counts.items()} if total else {}

    def requests_per_journey(self) -> int:
        return sum(step.count for step in self.steps)


# --------------------------------------------------------------------------- #
# Built-in scenarios that mirror real Kinora reading behaviour
# --------------------------------------------------------------------------- #


def steady_reader(*, pages: int = 20) -> Scenario:
    """A focused reader: open, then read ``pages`` pages at ~60s/page (§4.1).

    Between page turns the client polls buffer state a couple of times (the §4.3
    SSE/poll), so this journey is dominated by cheap buffer reads punctuated by
    page turns — the steady-state shape the buffer math is tuned for.
    """
    page_think = ThinkTime(60.0, ThinkShape.EXPONENTIAL)
    poll_think = ThinkTime(5.0, ThinkShape.UNIFORM, spread=0.4)
    steps: list[Step] = [Step(ReadEndpoint.OPEN_BOOK, think=ThinkTime(2.0, ThinkShape.FIXED))]
    for _ in range(pages):
        steps.append(Step(ReadEndpoint.BUFFER_STATE, count=2, think=poll_think))
        steps.append(Step(ReadEndpoint.PAGE_TURN, think=page_think))
    steps.append(Step(ReadEndpoint.CLOSE))
    return Scenario(name="steady_reader", steps=steps)


def skimming_reader(*, jumps: int = 10) -> Scenario:
    """A restless reader who flips/jumps fast — the §4.6 thrash adversary.

    Rapid jumps with short think times stress trajectory-instability handling
    (speculative cancels + re-promotion) far harder than steady reading.
    """
    quick = ThinkTime(3.0, ThinkShape.EXPONENTIAL)
    steps: list[Step] = [Step(ReadEndpoint.OPEN_BOOK, think=ThinkTime(1.0, ThinkShape.FIXED))]
    for _ in range(jumps):
        steps.append(Step(ReadEndpoint.JUMP, think=quick))
        steps.append(Step(ReadEndpoint.BUFFER_STATE, think=quick))
    steps.append(Step(ReadEndpoint.CLOSE))
    return Scenario(name="skimming_reader", steps=steps)


def directing_reader(*, pages: int = 8, notes: int = 3) -> Scenario:
    """A reader who reads *and* leaves director notes (§5.4 comment → regen).

    Comments POST to ``/sessions/{id}/comment`` and trigger a re-render, so this
    journey exercises the heaviest write path on the reading plane.
    """
    page_think = ThinkTime(45.0, ThinkShape.EXPONENTIAL)
    note_think = ThinkTime(20.0, ThinkShape.UNIFORM, spread=0.5)
    steps: list[Step] = [Step(ReadEndpoint.OPEN_BOOK, think=ThinkTime(2.0, ThinkShape.FIXED))]
    notes_left = notes
    for i in range(pages):
        steps.append(Step(ReadEndpoint.PAGE_TURN, think=page_think))
        if notes_left > 0 and i % 2 == 1:
            steps.append(Step(ReadEndpoint.COMMENT, think=note_think))
            notes_left -= 1
    steps.append(Step(ReadEndpoint.CLOSE))
    return Scenario(name="directing_reader", steps=steps)


#: The built-in scenario registry, keyed by name.
BUILTIN_SCENARIOS: dict[str, Scenario] = {
    s.name: s
    for s in (steady_reader(), skimming_reader(), directing_reader())
}


def get_scenario(name: str) -> Scenario:
    """Look up a built-in scenario by name (raises ``KeyError`` if unknown)."""
    return BUILTIN_SCENARIOS[name]
