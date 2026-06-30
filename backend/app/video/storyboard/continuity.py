"""Continuity hand-offs between consecutive shots (the §9.3 cut discipline).

Once the ordered shot list, coverage, and render modes exist, the planner threads
the *cuts* between shots: how each shot hands off from the one before it. This is
the storyboard-time analogue of the §9.3 ``video_continuation`` /
``first_and_last_frame`` discipline — a planning hint the render pipeline honours
when it later continues only from QA-passed endpoint frames.

Rules (deterministic):

- The first shot opens a scene → ``SCENE_START``, no anchor.
- Two consecutive shots in the **same beat** showing the **same locked
  character** are a continuous take → ``CONTINUOUS`` (the next shot shares the
  previous shot's last frame), and the next shot's render mode is upgraded to
  ``VIDEO_CONTINUATION``.
- Two consecutive shots that must land on / open from a matching composition
  (e.g. an ``INSERT`` or ``REACTION`` cutting back to a ``MASTER`` of the same
  character) are a graphic ``MATCH_FRAME`` hand-off → the next shot opens on the
  previous close (``FIRST_LAST_FRAME``).
- Everything else is a plain ``HARD_CUT`` (a new setup — a different beat, a
  location change, an establishing wide).

The upgrade only fires when there is a locked character to anchor on, so a
``text_to_video`` establishing wide is never spuriously made continuous.
"""

from __future__ import annotations

from app.agents.contracts import RenderMode

from .models import (
    CHARACTER_COVERAGE,
    ContinuityKind,
    ContinuityLink,
    ShotCoverage,
    StoryboardShot,
)


def link_continuity(shots: list[StoryboardShot]) -> list[StoryboardShot]:
    """Thread continuity hand-offs through an ordered shot list (in place-safe).

    Returns a new list of shots with each shot's ``continuity`` and (where a
    hand-off upgrades it) ``render_mode`` set. The input order is the edit order;
    shots are not reordered.
    """
    out: list[StoryboardShot] = []
    prev: StoryboardShot | None = None
    for shot in shots:
        link, upgraded_mode = _decide_link(prev, shot)
        updated = shot.model_copy(
            update={
                "continuity": link,
                "render_mode": upgraded_mode if upgraded_mode is not None else shot.render_mode,
            }
        )
        out.append(updated)
        prev = updated
    return out


def _decide_link(
    prev: StoryboardShot | None, shot: StoryboardShot
) -> tuple[ContinuityLink, RenderMode | None]:
    """The continuity link from ``prev`` to ``shot`` + an optional mode upgrade."""
    if prev is None:
        return ContinuityLink(kind=ContinuityKind.SCENE_START), None

    same_beat = prev.beat_id == shot.beat_id
    shared_char = _shared_locked_character(prev, shot)

    # Same beat, same on-screen character, both character-coverage roles → a
    # seam-free continuous take (continue from the previous accepted endpoint).
    if same_beat and shared_char and _both_character_roles(prev, shot):
        link = ContinuityLink(
            kind=ContinuityKind.CONTINUOUS,
            from_shot_id=prev.shot_id,
            shares_first_frame=True,
        )
        return link, RenderMode.VIDEO_CONTINUATION

    # A cut back to / from a matching composition on a shared character → a
    # graphic match-frame hand-off (open on the previous close).
    if shared_char and _is_match_pair(prev, shot):
        link = ContinuityLink(
            kind=ContinuityKind.MATCH_FRAME,
            from_shot_id=prev.shot_id,
            shares_first_frame=True,
        )
        return link, RenderMode.FIRST_LAST_FRAME

    return ContinuityLink(kind=ContinuityKind.HARD_CUT, from_shot_id=prev.shot_id), None


def _shared_locked_character(prev: StoryboardShot, shot: StoryboardShot) -> bool:
    """True when both shots reference at least one common locked entity.

    A continuous/match hand-off only makes sense when the same pinned appearance
    carries across the cut — otherwise there is nothing to continue from.
    """
    prev_refs = set(prev.intent.reference_entities) or set(prev.entities)
    shot_refs = set(shot.intent.reference_entities) or set(shot.entities)
    return bool(prev_refs & shot_refs)


def _both_character_roles(prev: StoryboardShot, shot: StoryboardShot) -> bool:
    return prev.coverage in CHARACTER_COVERAGE and shot.coverage in CHARACTER_COVERAGE


#: Coverage pairs that read as a graphic match cut (detail/reaction ↔ master).
_MATCH_ROLES: frozenset[ShotCoverage] = frozenset(
    {ShotCoverage.INSERT, ShotCoverage.REACTION, ShotCoverage.MASTER, ShotCoverage.POV}
)


def _is_match_pair(prev: StoryboardShot, shot: StoryboardShot) -> bool:
    """A match-frame cut: both ends are match-eligible and the coverage changes."""
    return (
        prev.coverage in _MATCH_ROLES
        and shot.coverage in _MATCH_ROLES
        and prev.coverage != shot.coverage
    )


__all__ = ["link_continuity"]
