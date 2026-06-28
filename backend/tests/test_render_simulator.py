"""The deterministic, zero-IO render-control-flow simulator (kinora.md §9.7).

Drives the same decision modules the live pipeline uses (state machine + decide_qa
+ decide_retry + plan_ladder + poison) over scripted scenarios. Every §9.7 edge
is reachable here without ffmpeg/DB/network — the engine's control-flow proof.
"""

from __future__ import annotations

from app.render.ladder import LadderAssets, Rung
from app.render.retry import RetryPolicy
from app.render.simulator import (
    ConflictOutcome,
    QAVerdict,
    RenderScenario,
    RenderSimulator,
    SceneScenario,
    simulate,
    simulate_scene,
)
from app.render.states import RenderState
from app.render.telemetry import EventKind

# --------------------------------------------------------------------------- #
# Happy path
# --------------------------------------------------------------------------- #


def test_passing_qa_accepts_full_video() -> None:
    report = simulate(RenderScenario(qa_sequence=[QAVerdict.passing()]))
    assert report.accepted
    assert report.rung is Rung.FULL_WAN
    assert report.attempts == 1
    assert report.video_seconds == 5.0
    # The §9.7 state path is the canonical happy walk.
    assert report.state_path() == [
        RenderState.CACHE_CHECK,
        RenderState.RENDERING,
        RenderState.QA,
        RenderState.ACCEPTED,
    ]


# --------------------------------------------------------------------------- #
# Repair loop → retry cap → degrade
# --------------------------------------------------------------------------- #


def test_identity_fail_every_attempt_degrades_after_cap() -> None:
    report = simulate(
        RenderScenario(
            qa_sequence=[QAVerdict.identity_fail()],  # repeats every attempt
            assets=LadderAssets(has_keyframe=True),
        )
    )
    assert report.degraded
    assert report.rung is Rung.KEN_BURNS_KEYFRAME
    assert report.attempts == 3  # cap=2 → initial + 2 repairs
    assert report.video_seconds == 15.0  # each attempt charged its seconds
    # A repair was scheduled on the non-final failing attempts.
    retries = [e for e in report.events if e.kind is EventKind.RETRY_SCHEDULED]
    assert len(retries) == 2


def test_repair_then_pass_accepts_on_second_attempt() -> None:
    report = simulate(
        RenderScenario(qa_sequence=[QAVerdict.style_fail(), QAVerdict.passing()])
    )
    assert report.accepted
    assert report.attempts == 2
    assert RenderState.REPAIR in report.state_path()


def test_motion_fail_routes_through_repair() -> None:
    report = simulate(
        RenderScenario(qa_sequence=[QAVerdict.motion_fail(), QAVerdict.passing()])
    )
    assert report.accepted
    assert report.attempts == 2


# --------------------------------------------------------------------------- #
# Budget / gate degradation
# --------------------------------------------------------------------------- #


def test_live_gate_off_degrades_with_zero_video_seconds() -> None:
    report = simulate(
        RenderScenario(live_feasible=False, assets=LadderAssets(has_keyframe=True))
    )
    assert report.degraded
    assert report.rung is Rung.KEN_BURNS_KEYFRAME
    assert report.video_seconds == 0.0
    assert report.attempts == 0
    assert RenderState.DEGRADED in report.state_path()


def test_budget_low_degrades_to_best_available_rung() -> None:
    report = simulate(
        RenderScenario(budget_low=True, assets=LadderAssets(has_page_illustration=True))
    )
    assert report.degraded
    assert report.rung is Rung.KEN_BURNS_ILLUSTRATION


def test_no_assets_degrades_to_audio_card() -> None:
    report = simulate(RenderScenario(live_feasible=False, assets=LadderAssets()))
    assert report.rung is Rung.AUDIO_TEXT_ONLY


# --------------------------------------------------------------------------- #
# Conflict (§7.2) routing
# --------------------------------------------------------------------------- #


def test_timeline_conflict_honor_then_pass_accepts() -> None:
    report = simulate(
        RenderScenario(
            qa_sequence=[QAVerdict.timeline_fail(), QAVerdict.passing()],
            conflict=ConflictOutcome(action="honor"),
        )
    )
    assert report.accepted
    assert RenderState.CONFLICT in report.state_path()
    assert report.attempts == 2


def test_timeline_conflict_surface_parks_the_shot() -> None:
    report = simulate(
        RenderScenario(
            qa_sequence=[QAVerdict.timeline_fail()],
            conflict=ConflictOutcome(action="surface"),
        )
    )
    assert report.final_state is RenderState.CONFLICT
    assert report.surfaced_conflict is True


def test_timeline_conflict_accept_clears_via_continuity() -> None:
    report = simulate(
        RenderScenario(
            qa_sequence=[QAVerdict.timeline_fail()],
            conflict=ConflictOutcome(action="accept"),
        )
    )
    assert report.accepted
    assert RenderState.CONFLICT in report.state_path()


# --------------------------------------------------------------------------- #
# Poison / dead-shot
# --------------------------------------------------------------------------- #


def test_repeated_crashes_quarantine_to_audio_card() -> None:
    # cap=2 → three attempts; crashing all three poisons (threshold 3) by the last.
    report = simulate(
        RenderScenario(
            raise_on_attempt=frozenset({0, 1, 2}),
            assets=LadderAssets(has_keyframe=True),
        )
    )
    assert report.degraded
    assert report.poisoned is True
    assert report.rung is Rung.AUDIO_TEXT_ONLY
    poison_events = [e for e in report.events if e.kind is EventKind.POISONED]
    assert len(poison_events) == 1


def test_pre_quarantined_shot_skips_straight_to_bottom_rung() -> None:
    report = simulate(RenderScenario(already_poisoned=True))
    assert report.degraded
    assert report.poisoned is True
    assert report.rung is Rung.AUDIO_TEXT_ONLY
    assert report.attempts == 0
    assert report.video_seconds == 0.0


def test_a_single_transient_crash_recovers_then_passes() -> None:
    # One crash on attempt 0 (not enough to poison), then a clean render.
    report = simulate(
        RenderScenario(
            raise_on_attempt=frozenset({0}),
            qa_sequence=[QAVerdict.passing()],
        )
    )
    assert report.accepted
    assert not report.poisoned


# --------------------------------------------------------------------------- #
# Determinism + custom policy
# --------------------------------------------------------------------------- #


def test_simulation_is_deterministic() -> None:
    scenario = RenderScenario(qa_sequence=[QAVerdict.identity_fail()])
    a = RenderSimulator().run(scenario)
    b = RenderSimulator().run(scenario)
    assert (a.final_state, a.rung, a.attempts, a.video_seconds) == (
        b.final_state,
        b.rung,
        b.attempts,
        b.video_seconds,
    )


def test_higher_cap_allows_more_repairs() -> None:
    report = simulate(
        RenderScenario(qa_sequence=[QAVerdict.identity_fail()]),
        policy=RetryPolicy(cap=4),
    )
    assert report.attempts == 5  # initial + 4 repairs
    assert report.video_seconds == 25.0


def test_event_trace_is_ordered_and_complete() -> None:
    report = simulate(RenderScenario(qa_sequence=[QAVerdict.passing()]))
    seqs = [e.seq for e in report.events]
    assert seqs == sorted(seqs)  # monotone sequence
    # A finished event closes every simulation.
    assert report.events[-1].kind is EventKind.SHOT_FINISHED


# --------------------------------------------------------------------------- #
# Scene-level simulation (the §9.6 DAG + per-shot control flow)
# --------------------------------------------------------------------------- #


async def test_scene_simulation_orders_continuation_and_sums_budget() -> None:
    scene = SceneScenario(
        shots=[
            {"shot_id": "a", "render_mode": "reference_to_video"},
            {"shot_id": "b", "render_mode": "video_continuation"},
            {"shot_id": "x", "render_mode": "text_to_video"},
        ],
        default=RenderScenario(qa_sequence=[QAVerdict.passing()]),
    )
    report = await simulate_scene(scene)
    assert set(report.reports) == {"a", "b", "x"}
    assert report.accepted_count == 3
    assert report.total_video_seconds == 15.0  # three 5s shots accepted
    # The independents (a, x) fan out in the first batch; b waits for a.
    assert report.batches[0] == ["a", "x"]
    assert report.max_parallelism == 2


async def test_scene_simulation_reports_ladder_distribution() -> None:
    scene = SceneScenario(
        shots=[
            {"shot_id": "live", "render_mode": "reference_to_video"},
            {"shot_id": "degraded", "render_mode": "text_to_video"},
        ],
        per_shot={
            "live": RenderScenario(qa_sequence=[QAVerdict.passing()]),
            "degraded": RenderScenario(
                live_feasible=False, assets=LadderAssets(has_keyframe=True)
            ),
        },
    )
    report = await simulate_scene(scene)
    dist = report.ladder_distribution()
    assert dist[Rung.FULL_WAN.value] == 1
    assert dist[Rung.KEN_BURNS_KEYFRAME.value] == 1
    assert report.accepted_count == 1
    assert report.degraded_count == 1


async def test_scene_simulation_blocks_continuation_on_degraded_predecessor() -> None:
    scene = SceneScenario(
        shots=[
            {"shot_id": "a", "render_mode": "reference_to_video"},
            {"shot_id": "b", "render_mode": "video_continuation"},
        ],
        per_shot={
            "a": RenderScenario(live_feasible=False, assets=LadderAssets(has_keyframe=True)),
        },
        default=RenderScenario(qa_sequence=[QAVerdict.passing()]),
    )
    report = await simulate_scene(scene)
    # 'a' degraded → 'b' (continuation) cannot extend an accepted endpoint.
    assert report.blocked == ["b"]
    assert "b" not in report.reports
