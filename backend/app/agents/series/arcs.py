"""Multi-volume character & relationship ARC tracking (§7, §8.5).

A character's *arc* is the shape of their dramatic change across a whole series:
``setup → rising → turn → climax → falling → resolution`` (the
:class:`~app.agents.contracts.ArcStage` ordering). The single-book canon graph
(§8.1) already records *state* (the sword is lost, the castle burned); the arc is
the orthogonal *trajectory* layer the Showrunner reasons about when planning a
multi-volume work.

Everything here is pure and deterministic — the arc is a **read model** computed
from structured :class:`~app.agents.contracts.ArcBeat` samples, the same way the
pacing curve and structure detector are. Two properties mirror §8.5 forgetting:

* an arc **advances monotonically** through the stage ordering (a beat can't move
  the resolved stage backwards — a regression is reported, not applied), while the
  *intensity* tracks the latest beat;
* an arc resolved **as of** a series position ``(volume, beat)`` reflects only the
  beats up to that point, so a time-travel read (the reader scrolls back) sees the
  arc exactly as it stood then — never a fact from "the future".
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from app.agents.contracts import (
    ARC_STAGE_ORDER,
    ArcBeat,
    ArcStage,
    ArcState,
    CharacterArc,
    RelationshipArc,
    RelationshipKind,
)

#: Rank of each stage in the canonical forward ordering (0 = setup, 5 = resolution).
_STAGE_RANK: dict[ArcStage, int] = {stage: i for i, stage in enumerate(ARC_STAGE_ORDER)}


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def stage_rank(stage: ArcStage) -> int:
    """The 0..5 progression rank of an arc stage (setup=0, resolution=5)."""
    return _STAGE_RANK[stage]


def _position_key(beat: ArcBeat) -> tuple[int, int]:
    """The series-position sort key for a beat: ``(volume_index, beat_index)``."""
    return (beat.volume_index, beat.beat_index)


def sort_arc_beats(beats: Iterable[ArcBeat]) -> list[ArcBeat]:
    """Return the beats in series order (volume-major, then beat index)."""
    return sorted(beats, key=_position_key)


def advance_arc(state: ArcState, beat: ArcBeat) -> ArcState:
    """Fold one beat into an arc-state, advancing it monotonically (§7).

    The resolved ``stage`` only ever moves *forward* in the
    :class:`ArcStage` ordering — a beat tagged with an earlier stage cannot rewind
    the arc (that would be a continuity regression; :func:`arc_regressions` reports
    such beats instead). ``intensity`` always tracks the newest beat. The position
    advances to the beat's ``(volume, beat)`` and ``beats_seen`` increments.

    Pure: returns a new :class:`ArcState`, never mutates ``state``.
    """
    new_stage = state.stage
    if stage_rank(beat.stage) > stage_rank(state.stage):
        new_stage = beat.stage
    return ArcState(
        stage=new_stage,
        intensity=_clamp01(beat.intensity),
        last_volume=beat.volume_index,
        last_beat=beat.beat_index,
        beats_seen=state.beats_seen + 1,
    )


def fold_arc(beats: Iterable[ArcBeat], *, start: ArcState | None = None) -> ArcState:
    """Fold a run of beats (in series order) into a single resolved arc-state."""
    state = start or ArcState()
    for beat in sort_arc_beats(beats):
        state = advance_arc(state, beat)
    return state


def arc_state_at(
    arc: CharacterArc | RelationshipArc,
    *,
    volume_index: int,
    beat_index: int,
) -> ArcState:
    """Resolve an arc's state *as of* a series position (§8.5 time-travel read).

    Only beats at or before ``(volume_index, beat_index)`` contribute, so scrolling
    back to an earlier beat resolves the arc to where it stood then — the same
    interval-scoping that keeps a retired fact invisible to forward generation.
    """
    cutoff = (volume_index, beat_index)
    seen = [b for b in sort_arc_beats(arc.beats) if _position_key(b) <= cutoff]
    return fold_arc(seen)


def current_arc_state(arc: CharacterArc | RelationshipArc) -> ArcState:
    """The arc-state after every recorded beat (the live read model)."""
    return fold_arc(arc.beats)


def arc_regressions(arc: CharacterArc | RelationshipArc) -> list[ArcBeat]:
    """The beats that would move the resolved stage *backwards* (§7 coherence).

    A well-formed arc has none: stages only advance. Any beat returned here marks
    a continuity problem the §13 eval harness (:mod:`app.agents.series.eval`)
    surfaces — e.g. a "climax" beat in Volume 1 followed by a "rising" beat in
    Volume 2 means the arc regressed and the plan needs attention.
    """
    regressions: list[ArcBeat] = []
    high_water = -1
    for beat in sort_arc_beats(arc.beats):
        rank = stage_rank(beat.stage)
        if rank < high_water:
            regressions.append(beat)
        else:
            high_water = rank
    return regressions


def is_monotonic(arc: CharacterArc | RelationshipArc) -> bool:
    """True iff the arc never regresses through the stage ordering (§7)."""
    return not arc_regressions(arc)


def build_character_arc(
    *,
    entity_key: str,
    name: str = "",
    beats: Iterable[ArcBeat] = (),
) -> CharacterArc:
    """Assemble a :class:`CharacterArc` from sampled beats (sorted; volumes derived)."""
    ordered = sort_arc_beats(beats)
    spanned = sorted({b.volume_index for b in ordered})
    return CharacterArc(
        entity_key=entity_key,
        name=name,
        beats=ordered,
        spanned_volumes=spanned,
    )


def _canonical_pair(a: str, b: str) -> tuple[str, str]:
    """The unordered entity pair as a canonical sorted tuple."""
    return (a, b) if a <= b else (b, a)


def build_relationship_arc(
    *,
    entity_a: str,
    entity_b: str,
    kind: RelationshipKind = RelationshipKind.NEUTRAL,
    beats: Iterable[ArcBeat] = (),
) -> RelationshipArc:
    """Assemble a :class:`RelationshipArc` for an (unordered) character pair."""
    ordered = sort_arc_beats(beats)
    spanned = sorted({b.volume_index for b in ordered})
    return RelationshipArc(
        entity_keys=_canonical_pair(entity_a, entity_b),
        kind=kind,
        beats=ordered,
        spanned_volumes=spanned,
    )


def arc_intensity_trajectory(arc: CharacterArc | RelationshipArc) -> list[float]:
    """The intensity value at each beat in series order — the arc's emotional shape."""
    return [_clamp01(b.intensity) for b in sort_arc_beats(arc.beats)]


def stage_progress(state: ArcState) -> float:
    """How far through the arc the resolved state is, in ``[0, 1]`` (setup=0, resolution=1)."""
    last = len(ARC_STAGE_ORDER) - 1
    return stage_rank(state.stage) / last if last else 0.0


def merge_arc_beats(
    existing: Sequence[ArcBeat], incoming: Iterable[ArcBeat]
) -> list[ArcBeat]:
    """Merge new beats into an arc, de-duplicating on series position (§8.7-style).

    A later beat at the same ``(volume, beat)`` position *replaces* the earlier one
    (a re-ingest of the same beat wins), keeping the merge idempotent for repeated
    plan runs over an unchanged series.
    """
    by_pos: dict[tuple[int, int], ArcBeat] = {_position_key(b): b for b in existing}
    for beat in incoming:
        by_pos[_position_key(beat)] = beat
    return sort_arc_beats(by_pos.values())


__all__ = [
    "advance_arc",
    "arc_intensity_trajectory",
    "arc_regressions",
    "arc_state_at",
    "build_character_arc",
    "build_relationship_arc",
    "current_arc_state",
    "fold_arc",
    "is_monotonic",
    "merge_arc_beats",
    "sort_arc_beats",
    "stage_progress",
    "stage_rank",
]
