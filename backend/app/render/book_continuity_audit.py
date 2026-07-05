"""Whole-book long-range continuity audit (the seventh crew role, kinora.md's
"the story is accurate" made concrete at book scale).

app.render.continuity_qa scores drift between ADJACENT shots inside one event.
This module walks an ENTIRE book's accepted shots in reading order and flags
persistence-dimension changes (wardrobe/setting/lighting/time_of_day) that are
neither motivated by the shot's own text nor preceded by a fresh establishing
shot far enough away to plausibly be the story moving on rather than an error.
Pure: no ffmpeg, no DB — the caller supplies already-loaded shot data.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal, Protocol

#: A far-apart change is presumed to be legitimate story development (not
#: drift) once this many beats have passed without comment — a full chapter
#: easily exceeds this, a same-scene flicker does not.
_FRESH_ESTABLISHING_GAP_BEATS = 15

_PERSISTENCE_DIMENSIONS: tuple[str, ...] = ("wardrobe", "setting", "lighting", "time_of_day")

#: Prose cues that mark a shot as genuinely *re-establishing* the scene (a new
#: chapter, a later time, a different place) rather than merely continuing it —
#: the same small deterministic cue-list style as event_director's
#: ``_FAST_CUES``/``_SLOW_CUES``/``_POSE_CUES``. A far-apart change is only
#: excused when the later shot's own text carries one of these signals; an
#: ordinary non-empty summary ("she walks on") is not enough on its own.
_FRESH_ESTABLISHING_CUES: tuple[str, ...] = (
    "chapter",
    "weeks later",
    "months later",
    "years later",
    "days later",
    "meanwhile",
    "in the meantime",
    "elsewhere",
    "new city",
    "different city",
    "another city",
    # Indirect time-passage idioms common in the campaign's 19th-century prose
    # (confirmed real gap: "Autumn had turned to winter by the time she
    # reached the mountains" matched none of the explicit-marker cues above).
    "time had passed",
    "years had passed",
    "since that day",
    "the following morning",
    "the following day",
    "the following week",
    "turned to winter",
    "turned to spring",
    "turned to summer",
    "turned to autumn",
    "turned to fall",
    # Deliberately NOT included, despite reading like time-skip language:
    # "by the time" and "gave way to" are ordinary subordinating/transition
    # constructions that show up constantly with NO scene/time reset at all
    # ("by the time she finished her tea..."; "her fear gave way to anger") —
    # unlike every cue above, which names an explicit chapter/place/season
    # change. Found by independent review: either phrase, present anywhere in
    # a far-apart shot's text for an unrelated reason, would fully suppress a
    # genuine continuity defect rather than merely lower its confidence.
)


class ShotLike(Protocol):
    """The minimal shape this module needs from a book's shots, in reading order."""

    shot_id: str
    beat_index: int
    wardrobe: str | None
    setting: str | None
    lighting: str | None
    time_of_day: str | None
    hand_off: str
    summary: str


@dataclass(frozen=True, slots=True)
class LongRangeDrift:
    from_shot_id: str
    to_shot_id: str
    dimension: str
    from_value: str
    to_value: str
    confidence: Literal["high", "low"]

    def describe(self) -> str:
        return (
            f"{self.dimension} drifted from {self.from_value!r} to {self.to_value!r} "
            f"between {self.from_shot_id} and {self.to_shot_id} "
            f"({self.confidence}-confidence, no motivated change found)"
        )


@dataclass(frozen=True, slots=True)
class BookContinuityReport:
    book_id: str
    drifts: tuple[LongRangeDrift, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        return not self.drifts


def _shot_text(shot: ShotLike) -> str:
    return f"{shot.hand_off} {shot.summary}".lower()


def _motivated(shot: ShotLike, new_value: str) -> bool:
    return new_value.lower() in _shot_text(shot)


def _reads_as_fresh_establishing(shot: ShotLike) -> bool:
    """True if the shot's own text signals a scene/time reset.

    A merely non-empty summary is not enough — "she walks on" says nothing
    about a new chapter, a later time, or a different place, so it must not
    excuse an unmotivated change the way "a new chapter opens, weeks later,
    in a different city" does.
    """
    text = _shot_text(shot)
    return any(cue in text for cue in _FRESH_ESTABLISHING_CUES)


def audit_book_continuity(
    book_id: str,
    shots_in_reading_order: Sequence[ShotLike],
    *,
    canon_snapshots_by_shot: dict[str, object] | None = None,
) -> BookContinuityReport:
    """Walk every shot in reading order; flag unmotivated long-range drift.

    ``canon_snapshots_by_shot`` (canon state at each shot's point in the story,
    §8.3) is accepted for the caller's future use (cross-checking a shot
    against a canon fact locked after its original render) but not yet
    required by the wardrobe/setting/lighting/time_of_day checks below, which
    operate on the shots' own directives.
    """
    drifts: list[LongRangeDrift] = []
    # One independent "last seen" slot per persistence dimension — a wardrobe
    # change doesn't reset what we last saw for lighting, etc.
    last_value: dict[str, tuple[str, ShotLike]] = {}
    for shot in shots_in_reading_order:
        for dimension in _PERSISTENCE_DIMENSIONS:
            value = getattr(shot, dimension, None)
            if value is None:
                continue
            prior = last_value.get(dimension)
            last_value[dimension] = (value, shot)
            if prior is None:
                continue  # first time this dimension is set — nothing to compare yet
            prior_value, prior_shot = prior
            if value == prior_value:
                continue
            if _motivated(shot, value):
                continue  # a named, motivated change — not drift
            gap = shot.beat_index - prior_shot.beat_index
            if gap >= _FRESH_ESTABLISHING_GAP_BEATS and _reads_as_fresh_establishing(shot):
                continue  # far enough + a fresh establishing shot — story moved on
            drifts.append(
                LongRangeDrift(
                    from_shot_id=prior_shot.shot_id,
                    to_shot_id=shot.shot_id,
                    dimension=dimension,
                    from_value=prior_value,
                    to_value=value,
                    confidence="high" if gap < _FRESH_ESTABLISHING_GAP_BEATS else "low",
                )
            )
    return BookContinuityReport(book_id=book_id, drifts=tuple(drifts))


__all__ = ["BookContinuityReport", "LongRangeDrift", "ShotLike", "audit_book_continuity"]
