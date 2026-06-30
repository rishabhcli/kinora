"""The pluggable reasoning seam for storyboard planning (``REASONING_PROVIDER``).

The planner is split into a *creative* call and a *deterministic* assembly. The
creative call — segment a passage into beats and propose a coverage plan per beat
— is the only part a real LLM (Qwen / OpenAI, per ``settings.reasoning_provider``)
would do; everything downstream (budget fitting, continuity hand-offs, validation)
is pure code. This module defines that seam as a :class:`ReasoningProvider`
protocol plus two network-free implementations:

- :class:`HeuristicReasoningProvider` — a deterministic default that segments on
  sentence boundaries and proposes coverage from the same prose signals the
  comprehension layer already uses. Production can swap an LLM-backed provider in
  without touching the engine.
- :class:`ScriptedReasoningProvider` — returns plans from a pre-built script,
  keyed by passage id. Used by the deterministic tests so the engine can be
  exercised end-to-end with **no network and a fixed creative output**.

A provider returns a :class:`ReasoningPlan`: an ordered list of beat plans, each
naming the text slice, the entities present, the suggested coverage roles, and a
tempo hint. The engine treats this as advice — it re-derives anything the
provider omits and always re-runs the deterministic budget/continuity/validation.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from app.agents.contracts import SceneTempo

from .models import Passage, ShotCoverage


class BeatPlan(BaseModel):
    """A provider's proposal for one beat: its text, entities, and coverage.

    ``coverage`` is the ordered list of editorial roles the provider suggests for
    this beat (one entry per shot it recommends). An empty list lets the engine
    decide coverage deterministically from the beat's signals.
    """

    model_config = ConfigDict(extra="ignore")

    beat_id: str
    text: str
    word_range: tuple[int, int] = (0, 0)
    page: int = 0
    entities: list[str] = Field(default_factory=list)
    tempo: SceneTempo = SceneTempo.SCENE
    mood: str | None = None
    subjective: bool = False
    pov_character: str | None = None
    coverage: list[ShotCoverage] = Field(default_factory=list)


class ReasoningPlan(BaseModel):
    """A provider's full proposal for a passage: ordered beat plans."""

    model_config = ConfigDict(extra="forbid")

    passage_id: str
    beats: list[BeatPlan] = Field(default_factory=list)


@runtime_checkable
class ReasoningProvider(Protocol):
    """The seam the engine calls to segment + propose coverage for a passage.

    Implementations may be an LLM (Qwen/OpenAI) or, in tests, a scripted/heuristic
    stand-in. The contract is async-only so a real provider can do network I/O;
    the deterministic implementations complete synchronously inside the coroutine.
    """

    async def plan_passage(self, passage: Passage) -> ReasoningPlan:
        """Propose an ordered beat plan for ``passage`` (segmentation + coverage)."""
        ...


class ScriptedReasoningProvider:
    """A :class:`ReasoningProvider` that returns pre-built plans by passage id.

    For deterministic tests: construct it with a mapping of ``passage_id`` →
    :class:`ReasoningPlan` (or a callable). It performs no segmentation and no
    network I/O — the test author controls the exact creative output so the
    engine's deterministic decomposition is what is under test.
    """

    def __init__(self, plans: dict[str, ReasoningPlan]) -> None:
        self._plans = dict(plans)
        self.calls: list[str] = []

    async def plan_passage(self, passage: Passage) -> ReasoningPlan:
        self.calls.append(passage.passage_id)
        try:
            return self._plans[passage.passage_id]
        except KeyError as exc:  # pragma: no cover - defensive
            raise KeyError(
                f"ScriptedReasoningProvider has no plan for passage {passage.passage_id!r}"
            ) from exc


class HeuristicReasoningProvider:
    """A deterministic, network-free default :class:`ReasoningProvider`.

    Segments a passage's ``text`` into beats on sentence boundaries (or passes
    through pre-segmented ``beats``), classifies each beat's tempo with the
    comprehension pacing heuristics, and leaves ``coverage`` empty so the engine's
    deterministic coverage planner decides. This is the production default until an
    LLM-backed provider is wired; the engine's behaviour is identical either way
    because the engine re-derives everything the provider leaves blank.
    """

    def __init__(self, words_per_beat: int = 45) -> None:
        # The narration-word target per segmented beat (a sentence-or-two).
        self._words_per_beat = max(8, words_per_beat)

    async def plan_passage(self, passage: Passage) -> ReasoningPlan:
        # Local import keeps the model dependency lazy and avoids a cycle.
        from .segmentation import segment_passage

        beats = passage.beats or segment_passage(
            passage, words_per_beat=self._words_per_beat
        )
        plans = [
            BeatPlan(
                beat_id=b.beat_id,
                text=b.text,
                word_range=b.word_range,
                page=b.page,
                entities=b.entities,
                tempo=b.tempo,
                mood=b.mood,
                subjective=b.subjective,
                pov_character=b.pov_character,
                coverage=[],  # engine decides coverage deterministically
            )
            for b in beats
        ]
        return ReasoningPlan(passage_id=passage.passage_id, beats=plans)


__all__ = [
    "BeatPlan",
    "HeuristicReasoningProvider",
    "ReasoningPlan",
    "ReasoningProvider",
    "ScriptedReasoningProvider",
]
