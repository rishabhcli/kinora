"""Continuity QA for an event film (Agent 1, WS3) — deterministic seam scoring.

Mirrors the §9.5 Critic discipline (concrete checks, not vibes; a hard failure
fails the seam even if everything else is fine) but for the *seam between two
shots* of one event: do they share geometry/aspect (no resolution jump), is the
later shot properly chained (a continuation/pose-landing mode with an explicit
last-frame hand-off), and is its dwell time sane. Failures route to a repair —
re-render at the film geometry, insert a bridging supplemental shot, or degrade.
"""

from __future__ import annotations

from app.agents.contracts import RenderMode
from app.render.continuity_qa import (
    SeamRepair,
    ShotGeometry,
    propose_supplemental_shot,
    route_event_continuity,
    score_event_continuity,
    score_seam,
)
from app.render.event_director import plan_event_script
from tests.test_render_event_director import _bridge_beats
from tests.test_render_support import make_slice

FILM = (720, 1280)


def _vertical(shot_id: str, dur: float = 5.0) -> ShotGeometry:
    return ShotGeometry(shot_id=shot_id, width=720, height=1280, duration_s=dur)


def _script():
    return plan_event_script(
        event_id="evt_001",
        book_id="book_demo",
        scene_id="scene_005",
        beats=_bridge_beats(),
        canon=make_slice(),
    )


def test_clean_seam_scores_perfect_and_ok() -> None:
    script = _script()
    seam = score_seam(
        _vertical(script.shots[0].shot_id),
        _vertical(script.shots[1].shot_id),
        script.shots[0],
        script.shots[1],
        film_size=FILM,
    )
    assert seam.geometry_match is True
    assert seam.aspect_ok is True
    assert seam.mode_chained is True
    assert seam.has_handoff is True
    assert seam.score == 1.0
    assert seam.ok is True


def test_resolution_jump_fails_seam_and_routes_to_regen() -> None:
    """A landscape (or differently-sized) shot mid-event is a resolution jump —
    a hard fail (like the Critic's wrong-face) routed to re-render at film size."""
    script = _script()
    landscape = ShotGeometry(
        shot_id=script.shots[1].shot_id, width=1920, height=1080, duration_s=5.0
    )
    seam = score_seam(
        _vertical(script.shots[0].shot_id),
        landscape,
        script.shots[0],
        script.shots[1],
        film_size=FILM,
    )
    assert seam.geometry_match is False
    assert seam.aspect_ok is False
    assert seam.ok is False
    assert route_event_continuity(seam) == SeamRepair.REGEN_CONTINUATION


def test_broken_chain_routes_to_insert_supplemental() -> None:
    """Geometry is fine but the later shot is a fresh, unanchored cut (no hand-off,
    text_to_video mid-event) → bridge it with a supplemental insert shot."""
    script = _script()
    cur = script.shots[1].model_copy(
        update={
            "render_mode": RenderMode.TEXT_TO_VIDEO,
            "directive": script.shots[1].directive.model_copy(
                update={"continues_from_shot_id": None, "last_frame_key": None, "hand_off": ""}
            ),
        }
    )
    seam = score_seam(
        _vertical(script.shots[0].shot_id),
        _vertical(cur.shot_id),
        script.shots[0],
        cur,
        film_size=FILM,
    )
    assert seam.geometry_match is True and seam.aspect_ok is True
    assert seam.mode_chained is False or seam.has_handoff is False
    assert seam.ok is False
    assert route_event_continuity(seam) == SeamRepair.INSERT_SUPPLEMENTAL


def test_unmotivated_180_flip_fails_seam_and_routes_to_insert() -> None:
    """Screen direction flips L→R to R→L with no reversal in the text — a 180° line
    cross. Geometry/chain are fine, so the fix is a bridging insert/cutaway."""
    script = _script()
    prev = script.shots[0].model_copy(
        update={
            "directive": script.shots[0].directive.model_copy(
                update={"screen_direction": "left_to_right", "motion_reversal": False}
            )
        }
    )
    cur = script.shots[1].model_copy(
        update={
            "directive": script.shots[1].directive.model_copy(
                update={"screen_direction": "right_to_left", "motion_reversal": False}
            )
        }
    )
    seam = score_seam(_vertical(prev.shot_id), _vertical(cur.shot_id), prev, cur, film_size=FILM)
    assert seam.geometry_match is True and seam.mode_chained is True and seam.has_handoff is True
    assert seam.direction_ok is False
    assert seam.ok is False
    assert route_event_continuity(seam) == SeamRepair.INSERT_SUPPLEMENTAL


def test_score_event_continuity_clean_event_is_ok() -> None:
    script = _script()
    geoms = [_vertical(s.shot_id, s.duration_s) for s in script.shots]
    report = score_event_continuity(script, geoms, film_size=FILM)
    assert report.geometry_uniform is True
    assert report.duration_ok is True
    assert all(s.ok for s in report.seams)
    assert report.ok is True
    assert report.action == SeamRepair.ACCEPT
    assert len(report.seams) == len(script.shots) - 1  # one seam between each pair


def test_score_event_continuity_flags_worst_action() -> None:
    script = _script()
    geoms = [_vertical(s.shot_id, s.duration_s) for s in script.shots]
    geoms[2] = ShotGeometry(
        shot_id=script.shots[2].shot_id, width=1920, height=1080, duration_s=5.0
    )
    report = score_event_continuity(script, geoms, film_size=FILM)
    assert report.ok is False
    assert report.geometry_uniform is False
    # A resolution jump is the most severe — it wins the event-level routing.
    assert report.action == SeamRepair.REGEN_CONTINUATION


def test_propose_supplemental_shot_bridges_two_shots() -> None:
    """The director's repair: a short bridging insert that continues from the prior
    shot and hands off into the next, so the hard cut reads as a deliberate beat."""
    script = _script()
    prev, nxt = script.shots[0], script.shots[1]
    bridge = propose_supplemental_shot(prev, nxt, book_id="book_demo", event_id="evt_001")
    assert bridge.render_mode == RenderMode.VIDEO_CONTINUATION
    assert bridge.directive.continues_from_shot_id == prev.shot_id
    assert bridge.directive.last_frame_key == f"lastframes/book_demo/{prev.shot_id}.png"
    assert 3.0 <= bridge.duration_s <= 8.0
    assert bridge.ordinal == prev.ordinal  # inserted right after prev (stable sort key)
    assert prev.shot_id in bridge.shot_id and "supp" in bridge.shot_id
