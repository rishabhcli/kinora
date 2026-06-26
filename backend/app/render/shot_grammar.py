"""Deterministic film grammar for an event (Agent 1, WS3 / §10).

Production logic that makes a multi-shot event read as deliberate filmmaking
rather than a slideshow:

* **shot-size progression** — the first shot of an event establishes **wide**, a
  pose-landing or intimate beat tightens to a **close** insert, and the interior
  plays **medium** — so the event has the establishing → medium → insert rhythm of
  a real cut sequence;
* **screen direction + the 180° rule** — a character's motion direction is held
  *consistent* across shots; it only flips when the text motivates a reversal
  ("she turns to face them"), which is exactly when crossing the action line is
  allowed. An *unmotivated* flip is a 180° violation the continuity QA flags.

Everything here is pure and string-driven, so the grammar is fully unit-testable
and the event director / continuity QA share one source of truth.
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum

from app.agents.contracts import Beat


class ScreenDirection(StrEnum):
    """Which way the action reads across frame (the axis the 180° rule protects)."""

    LEFT_TO_RIGHT = "left_to_right"
    RIGHT_TO_LEFT = "right_to_left"
    TOWARD = "toward"  # into the lens / closer
    AWAY = "away"  # into the distance / exiting
    NEUTRAL = "neutral"  # no clear motion direction


#: Lexical cues that pin a beat's screen direction. "Forward" motion (sprint/run/
#: cross/advance) reads left-to-right by film convention unless a side is named.
_L2R_CUES = ("to the right", "rightward", "left to right", "eastward")
_R2L_CUES = ("to the left", "leftward", "right to left", "westward")
_TOWARD_CUES = ("toward", "towards", "approaches", "advances", "closer", "into frame")
_AWAY_CUES = ("away", "recedes", "retreats", "into the distance", "exits", "vanishes")
_FORWARD_CUES = ("sprints", "runs", "races", "crosses", "charges", "dashes", "hurries")
#: Cues that motivate crossing the line — a reversal that is *not* a 180° error.
_REVERSAL_CUES = (
    "turns to face",
    "turns back",
    "doubles back",
    "spins around",
    "wheels around",
    "reverses",
    "the other way",
    "turns around",
)
#: Cues that pull the camera in to a close insert.
_CLOSE_CUES = ("face", "eyes", "hands", "close", "whisper", "tears", "trembling", "lips")
_WIDE_CUES = ("wide", "landscape", "vista", "horizon", "establish", "skyline", "panorama")
_POSE_CUES = ("turns to face", "final stand", "freeze", "lands on", "comes to rest", "stops dead")


def _text(beat: Beat) -> str:
    return f"{beat.summary or ''} {beat.mood or ''}".lower()


def is_motion_reversal(beat: Beat) -> bool:
    """Whether the beat motivates a change of screen direction (crosses the line)."""
    return any(cue in _text(beat) for cue in _REVERSAL_CUES)


def screen_direction_for_beat(beat: Beat) -> ScreenDirection:
    """The raw screen direction a beat's own text implies (``NEUTRAL`` if none)."""
    text = _text(beat)
    if any(cue in text for cue in _L2R_CUES):
        return ScreenDirection.LEFT_TO_RIGHT
    if any(cue in text for cue in _R2L_CUES):
        return ScreenDirection.RIGHT_TO_LEFT
    if any(cue in text for cue in _AWAY_CUES):
        return ScreenDirection.AWAY
    if any(cue in text for cue in _TOWARD_CUES):
        return ScreenDirection.TOWARD
    if any(cue in text for cue in _FORWARD_CUES):
        # Forward motion with no named side reads left-to-right by convention.
        return ScreenDirection.LEFT_TO_RIGHT
    return ScreenDirection.NEUTRAL


_OPPOSITES: dict[ScreenDirection, ScreenDirection] = {
    ScreenDirection.LEFT_TO_RIGHT: ScreenDirection.RIGHT_TO_LEFT,
    ScreenDirection.RIGHT_TO_LEFT: ScreenDirection.LEFT_TO_RIGHT,
    ScreenDirection.TOWARD: ScreenDirection.AWAY,
    ScreenDirection.AWAY: ScreenDirection.TOWARD,
}


def opposite_directions(a: ScreenDirection, b: ScreenDirection) -> bool:
    """Whether ``a`` and ``b`` are across-the-line opposites (a potential 180°)."""
    return _OPPOSITES.get(a) == b and b != ScreenDirection.NEUTRAL


def resolve_screen_directions(beats: Sequence[Beat]) -> list[ScreenDirection]:
    """Hold screen direction consistent across the event, flipping only on reversal.

    A beat with no directional cue *keeps* the running direction (continuity); a
    reversal cue flips it (a motivated line cross); an explicit new direction sets
    it. This is the resolved direction the shot grammar + 180° check read.
    """
    resolved: list[ScreenDirection] = []
    current = ScreenDirection.NEUTRAL
    for beat in beats:
        raw = screen_direction_for_beat(beat)
        if is_motion_reversal(beat) and current in _OPPOSITES:
            current = _OPPOSITES[current]
        elif raw is not ScreenDirection.NEUTRAL:
            current = raw
        resolved.append(current)
    return resolved


def violates_180(prev: ScreenDirection, cur: ScreenDirection, *, reversal: bool) -> bool:
    """A 180° violation: screen direction flips across the line *without* motivation."""
    return opposite_directions(prev, cur) and not reversal


def shot_size_for(ordinal: int, beat: Beat) -> str:
    """Pick a shot size for one beat following the establishing→insert grammar."""
    if ordinal == 0:
        return "wide"  # open the event on an establishing wide
    text = _text(beat)
    if any(cue in text for cue in _POSE_CUES) or any(cue in text for cue in _CLOSE_CUES):
        return "close"  # land a pose / intimate detail on a close insert
    if any(cue in text for cue in _WIDE_CUES):
        return "wide"  # a re-establishing wide (new vista / location)
    # Interior rhythm: alternate medium / close so the cut sequence breathes.
    return "medium" if ordinal % 2 == 1 else "close"


def shot_sizes_for_event(beats: Sequence[Beat]) -> list[str]:
    """The ordered shot sizes for an event's beats (establishing → medium → insert)."""
    return [shot_size_for(i, beat) for i, beat in enumerate(beats)]


__all__ = [
    "ScreenDirection",
    "is_motion_reversal",
    "opposite_directions",
    "resolve_screen_directions",
    "screen_direction_for_beat",
    "shot_size_for",
    "shot_sizes_for_event",
    "violates_180",
]
