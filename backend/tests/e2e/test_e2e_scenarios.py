"""End-to-end scenario tests driving the REAL render pipeline + ffmpeg ladder.

Each test runs a harness scenario through the real §9.7 state machine and the
real Ken-Burns degradation ladder (so the artifacts are genuine mp4s) with fully
faked providers — no DashScope, no database, no Redis, no network, and
``KINORA_LIVE_VIDEO`` never enabled. The scenarios are kept small (a couple of
shots) with a tight commit horizon so they stay fast and deterministic.

Skipped wholesale when no ffmpeg binary is present (matching
``tests.test_render_pipeline``).
"""

from __future__ import annotations

import pytest

from app.db.models.enums import ShotStatus
from app.e2e import assertions, scenarios
from app.e2e.assertions import HarnessAssertionError
from app.e2e.world import FakeWorld, WorldConfig
from app.render import degrade

pytestmark = pytest.mark.skipif(not degrade.ffmpeg_available(), reason="no ffmpeg binary available")


def _fast(**overrides: object) -> WorldConfig:
    """A tight-horizon config so the buffer fill renders only the first 1-2 shots."""
    return WorldConfig(commit_horizon_s=2.0, spec_horizon_s=6.0, **overrides)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Ingest (pure — no ffmpeg, but lives here with its siblings)
# --------------------------------------------------------------------------- #


def test_ingest_synthetic_book_produces_a_renderable_book() -> None:
    book = scenarios.ingest_synthetic_book()
    assert book.shots and book.beats and book.pages
    # Every shot is renderable: it has a beat + a span on a known page.
    for shot in book.shots:
        assert book.beat(shot.beat_id) is not None
        assert "word_range" in shot.source_span


# --------------------------------------------------------------------------- #
# Reader opens the book + turns pages → buffer stays ahead, sync covers narration
# --------------------------------------------------------------------------- #


async def test_reader_opens_and_buffer_stays_ahead() -> None:
    out = await scenarios.reader_opens_and_turns_pages(pages=1, config=_fast())
    assert out.results, "the buffer fill should have rendered at least one shot"

    # Every rendered shot reached a terminal, playable state.
    assertions.assert_shots_accepted(out.results)
    # The committed buffer is all rendered ahead of the reader.
    assertions.assert_buffer_ahead_of_reader(out.world, out.accepted_shot_ids())
    # Every shot's narration is covered by a monotonic sync map.
    for result in out.results:
        assertions.assert_sync_covers_narration(result)
    # No double-spend in the budget ledger.
    assertions.assert_no_double_spend(out.world)
    # The trace opens with a page turn then renders.
    assert out.trace is not None
    assert out.trace.kinds()[0] == "page_turn"


async def test_reader_path_is_all_full_video_when_live() -> None:
    out = await scenarios.reader_opens_and_turns_pages(pages=1, config=_fast())
    rungs = {r.rung for r in out.results}
    assert rungs == {"full_video"}
    assert all(r.status is ShotStatus.ACCEPTED for r in out.results)


# --------------------------------------------------------------------------- #
# Live gate off → real Ken-Burns ladder, still page-synced
# --------------------------------------------------------------------------- #


async def test_live_gate_off_rides_the_degradation_ladder() -> None:
    config = _fast(live_video=False)
    out = await scenarios.reader_opens_and_turns_pages(pages=1, config=config)
    assertions.assert_degradation_engaged(out.results)
    # Degraded clips are still terminal, playable, and page-synced at 0 video-s.
    for result in out.results:
        assert result.status is ShotStatus.DEGRADED
        assert result.video_seconds == 0.0
        assertions.assert_sync_covers_narration(result)
    assert out.world.budget.committed_total == 0.0


# --------------------------------------------------------------------------- #
# Director comments on a region → regenerate one shot (§5.4)
# --------------------------------------------------------------------------- #


async def test_director_comment_regenerates_the_shot() -> None:
    out = await scenarios.director_comments_on_region(config=_fast())
    assert len(out.results) == 2
    baseline, regen = out.results
    # Both are accepted, the regen ran the designer again, and it saw the note.
    assertions.assert_shots_accepted([baseline, regen])
    assert out.info["designer_calls"] >= 2
    assert out.info["last_notes_seen"] is True
    # The trace records the comment between the two renders.
    assert "director_comment" in (out.trace.kinds() if out.trace else [])


# --------------------------------------------------------------------------- #
# Budget exhausts mid-read → degrade, never double-spend
# --------------------------------------------------------------------------- #


async def test_budget_exhausts_then_degrades_without_double_spend() -> None:
    # Pool of 12 video-s, floor at 6: two live 5s renders, then degrade.
    config = WorldConfig(budget_s=12.0, budget_low_floor_s=6.0)
    world = FakeWorld(config=config)
    world.reader.focus_word = 0
    results = [await world.render_shot(s.shot_id) for s in world.book.shots[:4]]

    statuses = [r.status for r in results]
    assert statuses[:2] == [ShotStatus.ACCEPTED, ShotStatus.ACCEPTED]
    assert ShotStatus.DEGRADED in statuses[2:]
    # The committed ledger spent exactly the two live renders, no more.
    assert world.budget.committed_total == 10.0
    assertions.assert_no_double_spend(world)
    assertions.assert_degradation_engaged(results)


# --------------------------------------------------------------------------- #
# Provider fails over → repair / degrade, never crash
# --------------------------------------------------------------------------- #


async def test_provider_failover_degrades_gracefully() -> None:
    out = await scenarios.provider_fails_over(fail_first=1)
    result = out.results[0]
    # The generator was called and failed; the flow still reached a terminal,
    # playable state (degraded) rather than crashing.
    assert out.info["generator_calls"] >= 1
    assert result.status in {ShotStatus.DEGRADED, ShotStatus.ACCEPTED}
    assert result.clip_key is not None
    assertions.assert_sync_covers_narration(result)


# --------------------------------------------------------------------------- #
# Continuity conflict (§7.2) — auto-resolved (honour) vs surfaced
# --------------------------------------------------------------------------- #


async def test_conflict_auto_resolved_honours_canon() -> None:
    # No textual support + no director ⇒ honour canon ⇒ regen ⇒ accepted with a
    # decision record (the §7.2 autonomous path).
    out = await scenarios.continuity_conflict_arbitrated(
        showrunner_supported=False, director_present=False
    )
    result = out.results[0]
    assert out.info["continuity_calls"] >= 1
    assert out.info["showrunner_calls"] >= 1
    assert result.status is ShotStatus.ACCEPTED
    assert result.decision is not None


async def test_conflict_surfaced_to_present_director() -> None:
    # A present director with no textual support ⇒ the conflict is surfaced
    # (terminal CONFLICT) for the reader to arbitrate.
    out = await scenarios.continuity_conflict_arbitrated(
        showrunner_supported=False, director_present=True
    )
    result = out.results[0]
    assert result.status is ShotStatus.CONFLICT
    assert result.conflict is not None


# --------------------------------------------------------------------------- #
# Assertion helpers fail loudly when an invariant breaks
# --------------------------------------------------------------------------- #


async def test_buffer_assertion_detects_underrun() -> None:
    # Render nothing, then claim the buffer is ahead: it must flag the underrun.
    world = FakeWorld(config=_fast())
    world.reader.focus_word = 0
    assert world.committed_shots(), "the opening shots should be committed"
    with pytest.raises(HarnessAssertionError):
        assertions.assert_buffer_ahead_of_reader(world, accepted_shot_ids=[])
