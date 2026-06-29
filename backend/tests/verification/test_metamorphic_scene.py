"""Metamorphic tests over the §9.6 scene render DAG (``simulate_scene``).

The scene simulator runs a whole scene's shots through the §9.7 control flow on a
dependency graph (continuation shots wait for their predecessor's accepted
endpoint). The metamorphic relations here are *must-not-change* properties: the
scene's aggregate spend and outcome are a function of the shots and their
dependencies, not of incidental ordering, so

* **reordering independent shots** (no continuation edges) leaves the total
  video-seconds, the accepted/degraded counts, and the §12.4 ladder distribution
  exactly unchanged;
* **per-shot outcomes are stable** — the same shot id lands in the same state with
  the same spend no matter where it sits in the list.

This is the scene-scale analogue of the per-shot determinism property, and it is
the closest the suite gets to the §13 "reordering beats must not change canon"
relation expressible without the (network-bound) live pipeline.
"""

from __future__ import annotations

import asyncio

from hypothesis import given, settings
from hypothesis import strategies as st

from app.render.simulator import (
    QAVerdict,
    RenderScenario,
    SceneReport,
    SceneScenario,
    simulate_scene,
)
from app.verification.properties.strategies import ladder_assets


@st.composite
def independent_scenes(draw: st.DrawFn) -> SceneScenario:
    """A scene of fully independent shots (no continuation/dependency edges).

    Each shot is text-to-video-ish (no render_mode ⇒ no implicit continuation), so
    ``build_scene_graph`` wires no edges between them and any ordering is a valid
    topological order — the precondition for the reordering relation.
    """
    n = draw(st.integers(min_value=1, max_value=6))
    shots: list[dict[str, object]] = [{"shot_id": f"shot_{i:02d}"} for i in range(n)]
    per_shot: dict[str, RenderScenario] = {}
    for shot in shots:
        sid = str(shot["shot_id"])
        per_shot[sid] = RenderScenario(
            shot_id=sid,
            live_feasible=draw(st.booleans()),
            budget_low=draw(st.booleans()),
            assets=draw(ladder_assets),
            qa_sequence=draw(st.lists(_qa_verdicts(), min_size=1, max_size=3)),
            target_duration_s=draw(st.floats(min_value=1.0, max_value=15.0, allow_nan=False)),
        )
    return SceneScenario(shots=shots, per_shot=per_shot)


def _qa_verdicts() -> st.SearchStrategy[QAVerdict]:
    return st.sampled_from(
        [
            QAVerdict.passing(),
            QAVerdict.identity_fail(),
            QAVerdict.style_fail(),
            QAVerdict.motion_fail(),
            QAVerdict.timeline_fail(),
        ]
    )


def _run(scene: SceneScenario) -> SceneReport:
    return asyncio.run(simulate_scene(scene))


def _reversed(scene: SceneScenario) -> SceneScenario:
    return SceneScenario(shots=list(reversed(scene.shots)), per_shot=dict(scene.per_shot))


@given(independent_scenes())
@settings(max_examples=60)
def test_reordering_independent_shots_preserves_aggregate(
    scene: SceneScenario,
) -> None:
    """Metamorphic (§9.6): reordering independent shots can't change the scene total.

    Total video-seconds, accepted/degraded counts, and the ladder distribution are
    order-invariant when no shot depends on another.
    """
    base = _run(scene)
    other = _run(_reversed(scene))

    assert other.total_video_seconds == base.total_video_seconds
    assert other.accepted_count == base.accepted_count
    assert other.degraded_count == base.degraded_count
    assert other.ladder_distribution() == base.ladder_distribution()


@given(independent_scenes())
@settings(max_examples=60)
def test_per_shot_outcome_is_position_independent(scene: SceneScenario) -> None:
    """A shot's final state + spend is the same wherever it appears in the list."""
    base = _run(scene)
    other = _run(_reversed(scene))
    assert base.reports.keys() == other.reports.keys()
    for sid, report in base.reports.items():
        assert other.reports[sid].final_state == report.final_state
        assert other.reports[sid].video_seconds == report.video_seconds
        assert other.reports[sid].rung == report.rung


@given(independent_scenes())
@settings(max_examples=60)
def test_scene_total_equals_sum_of_shot_spend(scene: SceneScenario) -> None:
    """The scene budget draw is exactly the sum of its shots' spend (no double count)."""
    report = _run(scene)
    summed = round(sum(r.video_seconds for r in report.reports.values()), 3)
    assert report.total_video_seconds == summed


@given(independent_scenes())
@settings(max_examples=60)
def test_independent_scene_blocks_nothing(scene: SceneScenario) -> None:
    """With no continuation edges, no shot is ever ``blocked`` (nothing to wait on)."""
    report = _run(scene)
    assert report.blocked == []
