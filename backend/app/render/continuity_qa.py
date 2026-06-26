"""Deterministic continuity QA for an event film (Agent 1, WS3 / §9.5 patterns).

Where the §9.5 Critic scores one *clip* against the canon (identity, style,
timeline, motion), this module scores the **seam between two consecutive shots**
of one event — the join the reader actually perceives as a cut. The checks are
concrete and deterministic, not vibes, and follow the Critic's discipline: a hard
failure (a resolution/aspect jump) fails the seam outright, and the *kind* of
failure routes the repair (re-render at the film geometry, insert a bridging
supplemental shot, or degrade).

Everything here is **pure** — it takes already-measured :class:`ShotGeometry`
(probed once by the orchestrator) plus the planned :class:`~app.render.event_director.EventShot`s,
so the scoring and routing are exhaustively unit-testable with no ffmpeg.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from statistics import fmean

from app.agents.contracts import Camera, RenderMode
from app.core.logging import get_logger
from app.render.event_director import (
    MAX_SHOT_S,
    MIN_SHOT_S,
    ContinuityDirective,
    EventScript,
    EventShot,
)
from app.storage.object_store import keys

logger = get_logger("app.render.continuity_qa")

#: Render modes that read as a *chained* continuation mid-event (vs. a fresh cut).
_CHAINED_MODES = frozenset(
    {RenderMode.VIDEO_CONTINUATION, RenderMode.FIRST_LAST_FRAME, RenderMode.REFERENCE_TO_VIDEO}
)
#: How close a clip's aspect must be to the film aspect to count as "no jump".
_ASPECT_TOL = 0.02
#: Seam-score weights (sum to 1.0); all four must hold for the seam to pass.
_W_GEOMETRY, _W_ASPECT, _W_MODE, _W_HANDOFF = 0.30, 0.30, 0.20, 0.20


class SeamRepair(StrEnum):
    """How a failed seam is routed back into the event director's editing pass."""

    ACCEPT = "accept"
    #: A short bridging shot is inserted so a hard cut reads as a deliberate beat.
    INSERT_SUPPLEMENTAL = "insert_supplemental"
    #: The later shot is re-rendered as a proper continuation at the film geometry.
    REGEN_CONTINUATION = "regen_continuation"
    #: The seam is unsalvageable — fall to the Ken-Burns hold (§4.4 ladder).
    DEGRADE = "degrade"


#: Severity order for picking the event-level action from its seams.
_SEVERITY: dict[SeamRepair, int] = {
    SeamRepair.ACCEPT: 0,
    SeamRepair.INSERT_SUPPLEMENTAL: 1,
    SeamRepair.REGEN_CONTINUATION: 2,
    SeamRepair.DEGRADE: 3,
}


@dataclass(frozen=True, slots=True)
class ShotGeometry:
    """A rendered shot's measured geometry — the QA input (probed by the caller)."""

    shot_id: str
    width: int
    height: int
    duration_s: float


@dataclass(frozen=True, slots=True)
class SeamQuality:
    """The scored join between two consecutive shots of an event."""

    from_shot_id: str
    to_shot_id: str
    geometry_match: bool
    aspect_ok: bool
    mode_chained: bool
    has_handoff: bool
    score: float
    ok: bool


@dataclass(frozen=True, slots=True)
class EventContinuityReport:
    """The whole event's continuity verdict + the single recommended repair."""

    event_id: str
    seams: list[SeamQuality] = field(default_factory=list)
    geometry_uniform: bool = True
    duration_ok: bool = True
    score: float = 1.0
    ok: bool = True
    action: SeamRepair = SeamRepair.ACCEPT


def _aspect(geom: ShotGeometry) -> float:
    return geom.width / geom.height if geom.height else 0.0


def score_seam(
    prev_geom: ShotGeometry,
    cur_geom: ShotGeometry,
    prev_shot: EventShot,
    cur_shot: EventShot,
    *,
    film_size: tuple[int, int],
) -> SeamQuality:
    """Score the join ``prev → cur`` against the four continuity checks (pure)."""
    film_aspect = film_size[0] / film_size[1]
    geometry_match = (prev_geom.width, prev_geom.height) == (cur_geom.width, cur_geom.height)
    aspect_ok = (
        abs(_aspect(prev_geom) - film_aspect) < _ASPECT_TOL
        and abs(_aspect(cur_geom) - film_aspect) < _ASPECT_TOL
    )
    mode_chained = cur_shot.render_mode in _CHAINED_MODES
    has_handoff = cur_shot.directive.continues_from_shot_id == prev_shot.shot_id

    score = (
        _W_GEOMETRY * geometry_match
        + _W_ASPECT * aspect_ok
        + _W_MODE * mode_chained
        + _W_HANDOFF * has_handoff
    )
    ok = geometry_match and aspect_ok and mode_chained and has_handoff
    return SeamQuality(
        from_shot_id=prev_shot.shot_id,
        to_shot_id=cur_shot.shot_id,
        geometry_match=geometry_match,
        aspect_ok=aspect_ok,
        mode_chained=mode_chained,
        has_handoff=has_handoff,
        score=round(score, 4),
        ok=ok,
    )


def route_event_continuity(seam: SeamQuality) -> SeamRepair:
    """Map a failed seam to its repair by *which* check failed (§9.5 routing)."""
    if seam.ok:
        return SeamRepair.ACCEPT
    geometry_fail = not (seam.geometry_match and seam.aspect_ok)
    chain_fail = not (seam.mode_chained and seam.has_handoff)
    if geometry_fail and chain_fail:
        return SeamRepair.DEGRADE  # everything wrong — fall to the Ken-Burns hold
    if geometry_fail:
        return SeamRepair.REGEN_CONTINUATION  # a resolution/aspect jump
    return SeamRepair.INSERT_SUPPLEMENTAL  # a hard, unanchored cut


def score_event_continuity(
    script: EventScript,
    geometries: list[ShotGeometry],
    *,
    film_size: tuple[int, int],
) -> EventContinuityReport:
    """Score every seam of the event + roll up to one verdict + repair action."""
    geom_by_id = {g.shot_id: g for g in geometries}
    seams: list[SeamQuality] = []
    for prev_shot, cur_shot in zip(script.shots, script.shots[1:], strict=False):
        prev_geom = geom_by_id.get(prev_shot.shot_id)
        cur_geom = geom_by_id.get(cur_shot.shot_id)
        if prev_geom is None or cur_geom is None:
            continue
        seams.append(score_seam(prev_geom, cur_geom, prev_shot, cur_shot, film_size=film_size))

    geometry_uniform = len({(g.width, g.height) for g in geometries}) <= 1
    duration_ok = all(MIN_SHOT_S <= g.duration_s <= MAX_SHOT_S for g in geometries)
    action = max(
        (route_event_continuity(s) for s in seams),
        key=lambda a: _SEVERITY[a],
        default=SeamRepair.ACCEPT,
    )
    ok = bool(seams) and all(s.ok for s in seams) and geometry_uniform and duration_ok
    # A single-shot event (no seams) is trivially continuous.
    if not seams:
        ok = geometry_uniform and duration_ok
    score = round(fmean(s.score for s in seams), 4) if seams else 1.0

    report = EventContinuityReport(
        event_id=script.event_id,
        seams=seams,
        geometry_uniform=geometry_uniform,
        duration_ok=duration_ok,
        score=score,
        ok=ok,
        action=action,
    )
    if not ok:
        logger.info(
            "event.continuity_flag",
            event_id=script.event_id,
            action=action.value,
            score=score,
            geometry_uniform=geometry_uniform,
        )
    return report


def propose_supplemental_shot(
    prev_shot: EventShot,
    next_shot: EventShot,
    *,
    book_id: str,
    event_id: str,
) -> EventShot:
    """Build a short bridging insert so a failed cut reads as a deliberate beat.

    The supplemental continues from ``prev_shot``'s accepted last frame and hands
    off into ``next_shot`` — the director's "generate a supplemental shot when
    continuity QA fails" repair. Its ``ordinal`` matches ``prev_shot`` so a stable
    re-sort drops it directly after the shot it bridges from.
    """
    return EventShot(
        shot_id=f"{prev_shot.shot_id}_supp",
        beat_id=prev_shot.beat_id,
        ordinal=prev_shot.ordinal,
        render_mode=RenderMode.VIDEO_CONTINUATION,
        summary=f"Bridging insert: continue from {prev_shot.shot_id} into {next_shot.shot_id}.",
        camera=Camera(move="static", speed="slow", shot_size="medium"),
        duration_s=MIN_SHOT_S,
        source_span=prev_shot.source_span,
        directive=ContinuityDirective(
            wardrobe=prev_shot.directive.wardrobe,
            setting=prev_shot.directive.setting,
            lighting=prev_shot.directive.lighting,
            time_of_day=prev_shot.directive.time_of_day,
            camera_logic="bridging insert",
            hand_off=f"bridge into: {(next_shot.summary or '').strip()[:60]}",
            continues_from_shot_id=prev_shot.shot_id,
            last_frame_key=keys.lastframe(book_id, prev_shot.shot_id),
        ),
    )


__all__ = [
    "EventContinuityReport",
    "SeamQuality",
    "SeamRepair",
    "ShotGeometry",
    "propose_supplemental_shot",
    "route_event_continuity",
    "score_event_continuity",
    "score_seam",
]
