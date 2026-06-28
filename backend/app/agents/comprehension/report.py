"""Book-level comprehension telemetry (§9.1 verification / observability).

Aggregates a comprehended beat sequence into a compact, inspectable report: the
POV distribution (is this a single-POV or multi-POV book?), whether the prose is
linear or uses flashbacks/flash-forwards, the pacing-tempo histogram, the share
of unreliable narration, and dialogue/device counts. It is pure and cheap — a
natural addition to the ingest result and a quick sanity check that the
comprehension passes fired sensibly on a real book.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict, Field

from app.agents.contracts import (
    Beat,
    DiscourseMode,
    NarrativePerson,
    SceneTempo,
    TimePosition,
)


class ComprehensionReport(BaseModel):
    """A compact, inspectable summary of a book's deep comprehension."""

    model_config = ConfigDict(extra="forbid")

    num_beats: int = 0
    #: POV person → beat count (multi-POV iff more than one marked person).
    pov_distribution: dict[str, int] = Field(default_factory=dict)
    #: Distinct resolved POV characters across the book.
    pov_characters: list[str] = Field(default_factory=list)
    multi_pov: bool = False
    #: Beats whose telling is flagged unreliable.
    unreliable_beats: int = 0
    #: True iff story-order equals narrative-order everywhere (no time-shifts).
    linear: bool = True
    flashback_beats: int = 0
    flashforward_beats: int = 0
    #: SceneTempo value → beat count.
    tempo_histogram: dict[str, int] = Field(default_factory=dict)
    #: DiscourseMode value → beat count.
    discourse_histogram: dict[str, int] = Field(default_factory=dict)
    total_dialogue_lines: int = 0
    attributed_dialogue_lines: int = 0
    total_devices: int = 0


def summarize_comprehension(beats: Sequence[Beat]) -> ComprehensionReport:
    """Aggregate a comprehended beat sequence into a :class:`ComprehensionReport`.

    Expects beats already run through :func:`enrich_sequence` (so ``story_time``
    is populated); on raw beats it still produces a valid report from the
    default-neutral fields.
    """
    if not beats:
        return ComprehensionReport()

    pov_counts: Counter[str] = Counter()
    tempo_counts: Counter[str] = Counter()
    discourse_counts: Counter[str] = Counter()
    pov_chars: list[str] = []
    seen_chars: set[str] = set()
    unreliable = 0
    flashbacks = 0
    flashforwards = 0
    dlg_total = 0
    dlg_attr = 0
    devices = 0
    linear = True

    for i, beat in enumerate(beats):
        pov_counts[beat.pov.value] += 1
        tempo_counts[beat.tempo.value] += 1
        discourse_counts[beat.discourse.value] += 1
        if beat.unreliable:
            unreliable += 1
        if beat.pov_character and beat.pov_character not in seen_chars:
            seen_chars.add(beat.pov_character)
            pov_chars.append(beat.pov_character)
        pos = beat.story_time.position
        if pos is TimePosition.FLASHBACK:
            flashbacks += 1
        elif pos is TimePosition.FLASHFORWARD:
            flashforwards += 1
        if beat.story_time.order != i:
            linear = False
        for line in beat.dialogue:
            dlg_total += 1
            if line.speaker:
                dlg_attr += 1
        devices += len(beat.devices)

    # Multi-POV is about whose VANTAGE, not the limited/omniscient distinction:
    # collapse the three third-person variants into one "third" family, then a
    # book is multi-POV iff more than one person-family is marked OR more than one
    # distinct focal character is tracked.
    families = {_pov_family(p) for p in pov_counts} - {None}
    multi_pov = len(families) > 1 or len(pov_chars) > 1

    return ComprehensionReport(
        num_beats=len(beats),
        pov_distribution=dict(pov_counts),
        pov_characters=pov_chars,
        multi_pov=multi_pov,
        unreliable_beats=unreliable,
        linear=linear,
        flashback_beats=flashbacks,
        flashforward_beats=flashforwards,
        tempo_histogram=dict(tempo_counts),
        discourse_histogram=dict(discourse_counts),
        total_dialogue_lines=dlg_total,
        attributed_dialogue_lines=dlg_attr,
        total_devices=devices,
    )


def _pov_family(person: str) -> str | None:
    """Collapse a POV person to its vantage family for the multi-POV decision."""
    if person == NarrativePerson.UNKNOWN.value:
        return None
    if person == NarrativePerson.FIRST.value:
        return "first"
    if person == NarrativePerson.SECOND.value:
        return "second"
    return "third"  # both third-limited and third-omniscient


def dominant_tempo(report: ComprehensionReport) -> SceneTempo:
    """The most common pacing tempo in a report (SCENE when empty/tied-low)."""
    if not report.tempo_histogram:
        return SceneTempo.SCENE
    best = max(report.tempo_histogram, key=lambda k: report.tempo_histogram[k])
    try:
        return SceneTempo(best)
    except ValueError:
        return SceneTempo.SCENE


def dominant_discourse(report: ComprehensionReport) -> DiscourseMode:
    """The most common discourse mode in a report (NARRATION when empty)."""
    if not report.discourse_histogram:
        return DiscourseMode.NARRATION
    best = max(report.discourse_histogram, key=lambda k: report.discourse_histogram[k])
    try:
        return DiscourseMode(best)
    except ValueError:
        return DiscourseMode.NARRATION


__all__ = [
    "ComprehensionReport",
    "dominant_discourse",
    "dominant_tempo",
    "summarize_comprehension",
]
