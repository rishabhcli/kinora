"""Deterministic coverage planning: a beat → its ordered shot roles + render modes.

Classic continuity editing covers a beat from complementary angles rather than as
one flat take. This module turns a beat (its tempo, entities, dialogue, and
subjectivity) into an ordered list of :class:`ShotCoverage` roles — the editorial
spine of the storyboard — and maps each role onto a suggested §9.3
:class:`RenderMode`. Both steps are pure functions so the decomposition is fully
unit-testable and a reasoning provider can override the role list when it wants.

Coverage rules (deterministic, density-aware):
- A beat that opens on a new location with no character earns an ``establishing``
  wide (→ ``text_to_video``).
- Every beat earns a ``master`` of its principal action.
- A beat with attributed dialogue between two+ speakers earns a ``reaction`` on
  the listener (shot/reverse-shot), budget permitting.
- A beat flagged subjective/interiority earns a ``pov`` from its vantage character.
- A beat the prose lingers on (PAUSE) or names a salient prop earns an ``insert``.
- An ELLIPSIS beat (a time jump) collapses to a single ``transition`` bridge.

How *many* shots a beat earns is bounded by its tempo density and the remaining
shot budget — that allocation lives in :mod:`app.video.storyboard.budget`; this
module only proposes the *ordered candidate roles* (richest first), and the budget
trims from the tail.
"""

from __future__ import annotations

from app.agents.comprehension.dialogue import attribute_dialogue
from app.agents.contracts import RenderMode, SceneTempo

from .models import CanonContext, PassageBeat, ShotCoverage

#: The §9.3 render mode each coverage role suggests, *before* continuity refinement.
#: Continuity (a prev-accepted continuous take, an exact-pose hand-off) can later
#: upgrade a REFERENCE_TO_VIDEO master to VIDEO_CONTINUATION / FIRST_LAST_FRAME.
_COVERAGE_DEFAULT_MODE: dict[ShotCoverage, RenderMode] = {
    ShotCoverage.ESTABLISHING: RenderMode.TEXT_TO_VIDEO,
    ShotCoverage.MASTER: RenderMode.REFERENCE_TO_VIDEO,
    ShotCoverage.INSERT: RenderMode.REFERENCE_TO_VIDEO,
    ShotCoverage.REACTION: RenderMode.REFERENCE_TO_VIDEO,
    ShotCoverage.POV: RenderMode.REFERENCE_TO_VIDEO,
    ShotCoverage.TRANSITION: RenderMode.TEXT_TO_VIDEO,
}


def speakers_in_beat(beat: PassageBeat) -> list[str]:
    """Distinct named speakers in the beat's dialogue, in first-appearance order."""
    attrs = attribute_dialogue(beat.text)
    seen: list[str] = []
    for attr in attrs:
        name = (attr.speaker or "").strip()
        if name and name not in seen:
            seen.append(name)
    return seen


def _has_locked_character(beat: PassageBeat, context: CanonContext) -> bool:
    """True when at least one of the beat's entities has a locked reference."""
    return any(context.is_locked(e) for e in beat.entities)


def _opens_new_location(beat: PassageBeat, context: CanonContext, *, is_first: bool) -> bool:
    """Heuristic: the first beat of a passage with a known location establishes it.

    A passage's opening beat sets the stage; a location in context with no
    locked character to anchor on reads as an establishing wide.
    """
    return is_first and context.location is not None and not _has_locked_character(
        beat, context
    )


def plan_coverage(
    beat: PassageBeat,
    context: CanonContext,
    *,
    is_first: bool = False,
) -> list[ShotCoverage]:
    """Ordered, deterministic candidate coverage roles for a beat (richest first).

    The list is *aspirational*: the budget allocator (which knows the remaining
    shot ceiling and the tempo density) trims it from the tail, always keeping the
    head. The head is therefore the single most important shot of the beat — the
    establishing wide if the beat opens a location with no character, the master
    otherwise — so a beat trimmed to one shot still reads correctly.
    """
    # An explicit time-jump collapses to a single transition bridge.
    if beat.tempo is SceneTempo.ELLIPSIS:
        return [ShotCoverage.TRANSITION]

    roles: list[ShotCoverage] = []

    establishing = _opens_new_location(beat, context, is_first=is_first)
    if establishing:
        roles.append(ShotCoverage.ESTABLISHING)

    # The master is the spine; it is the head when there is no establishing wide.
    roles.append(ShotCoverage.MASTER)

    # Subjective/interiority → a POV from the vantage character.
    if beat.subjective:
        roles.append(ShotCoverage.POV)

    # Two+ speakers → a reaction on the listener (shot/reverse-shot).
    if len(speakers_in_beat(beat)) >= 2:
        roles.append(ShotCoverage.REACTION)

    # A lingering PAUSE earns a detail insert.
    if beat.tempo is SceneTempo.PAUSE:
        roles.append(ShotCoverage.INSERT)

    return roles


def render_mode_for(coverage: ShotCoverage, beat: PassageBeat, context: CanonContext) -> RenderMode:
    """The suggested §9.3 render mode for a coverage role (pre-continuity).

    Establishing/transition wides with no locked character stay ``text_to_video``;
    any character-bearing role on a locked entity locks appearance with
    ``reference_to_video`` (the face-drift primitive). When the beat has entities
    but none are locked yet, character roles still fall back to ``text_to_video``
    (nothing to pin to).
    """
    default = _COVERAGE_DEFAULT_MODE[coverage]
    if default is RenderMode.TEXT_TO_VIDEO:
        return default
    # A character-bearing role: only lock if there is a locked reference.
    if _has_locked_character(beat, context):
        return RenderMode.REFERENCE_TO_VIDEO
    return RenderMode.TEXT_TO_VIDEO


def entities_for(coverage: ShotCoverage, beat: PassageBeat, context: CanonContext) -> list[str]:
    """Which canon entities are in frame for a coverage role.

    An establishing/transition wide is the empty-or-location frame (no characters);
    a master shows every entity in the beat; a reaction/pov narrows to the vantage
    character when one is known, else the beat's entities. Every returned key is a
    member of the beat's entities (the no-invent guardrail) — a vantage character
    outside the beat is dropped.
    """
    beat_entities = list(beat.entities)
    if coverage in (ShotCoverage.ESTABLISHING, ShotCoverage.TRANSITION):
        return []
    if coverage in (ShotCoverage.REACTION, ShotCoverage.POV):
        pov = beat.pov_character
        if pov and pov in beat_entities:
            return [pov]
    return beat_entities


__all__ = [
    "entities_for",
    "plan_coverage",
    "render_mode_for",
    "speakers_in_beat",
]
