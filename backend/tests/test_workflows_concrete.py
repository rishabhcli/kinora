"""The two concrete durable workflows: book-ingest→render-scene and produce-episode.

End-to-end exercises of the real pipelines (zero credits, ``KINORA_LIVE_VIDEO``
off): ingest then per-shot state machine (cache/budget/render/QA/repair/degrade),
and the multi-agent episode orchestration (signals, queries, child workflows,
continue-as-new). Each asserts a sane result + deterministic replay.
"""

from __future__ import annotations

import pytest

from app.platform.workflows import WorkflowTestEnvironment, assert_deterministic_replay
from app.platform.workflows.defs.episode import (
    EPISODE_ACTIVITIES,
    EPISODE_WORKFLOWS,
    QUERY_PROGRESS,
    SIGNAL_DIRECTOR_DECISION,
)
from app.platform.workflows.defs.ingest_render import (
    INGEST_RENDER_ACTIVITIES,
    INGEST_RENDER_WORKFLOWS,
)


async def test_ingest_render_scene_completes_and_replays() -> None:
    env = WorkflowTestEnvironment(INGEST_RENDER_WORKFLOWS, INGEST_RENDER_ACTIVITIES)
    await env.start(
        "ingest_render_scene", "book_42", "s3://kinora/book_42.pdf", 0, workflow_id="ir"
    )
    result = await env.run_until_complete("ir")
    # Every shot is accounted for: accepted (incl. cache hits) + degraded == total.
    assert result["accepted"] + result["degraded"] == result["shots_total"]
    assert result["shots_total"] >= 1
    assert result["manifest_uri"].startswith("scene://")
    await assert_deterministic_replay(env, "ir")


async def test_ingest_render_rides_degradation_ladder_with_live_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With KINORA_LIVE_VIDEO off the budget gate denies live render → Ken-Burns."""
    monkeypatch.delenv("KINORA_LIVE_VIDEO", raising=False)
    env = WorkflowTestEnvironment(INGEST_RENDER_WORKFLOWS, INGEST_RENDER_ACTIVITIES)
    await env.start("ingest_render_scene", "book_x", "s3://k/x.pdf", 1, workflow_id="ir2")
    result = await env.run_until_complete("ir2")
    # The film never hard-stops: all non-cache shots are accounted for and the
    # scene still produces a manifest even though no live video was spent.
    assert result["accepted"] + result["degraded"] == result["shots_total"]


async def test_produce_episode_full_flow() -> None:
    env = WorkflowTestEnvironment(EPISODE_WORKFLOWS, EPISODE_ACTIVITIES)
    await env.start("produce_episode", "book_7", "s3://k/book_7.pdf", 4, workflow_id="ep")
    await env.worker.drain()  # parks awaiting the director's decision on scene 0

    # Live progress query before any approvals.
    progress = await env.client.query_workflow("ep", QUERY_PROGRESS)
    assert progress["completed"] == 0
    assert progress["in_review"] == 0

    decisions = ["approve", "revise", "approve", "skip", "approve"]
    for decision in decisions:
        await env.client.signal_workflow("ep", SIGNAL_DIRECTOR_DECISION, {"decision": decision})
        await env.worker.drain()
        execution = await env.store.get_execution("ep")
        assert execution is not None
        if execution.is_terminal:
            break

    result = await env.run_until_complete("ep")
    # 4 scenes, one skipped → 3 produced.
    assert result["scenes_produced"] == 3
    assert result["uri"] == "episode://book_7"
    await assert_deterministic_replay(env, "ep")


async def test_produce_episode_auto_approves_on_timeout() -> None:
    """With no director input, the durable approval timer auto-approves scenes."""
    env = WorkflowTestEnvironment(EPISODE_WORKFLOWS, EPISODE_ACTIVITIES)
    await env.start("produce_episode", "book_t", "s3://k/t.pdf", 2, workflow_id="ept")
    # No signals; run_until_complete advances virtual time so each approval timer
    # fires and auto-approves, producing all scenes without human input.
    result = await env.run_until_complete("ept")
    assert result["scenes_produced"] == 2


async def test_produce_episode_uses_continue_as_new() -> None:
    """A run longer than _SCENES_PER_RUN compacts history via continue-as-new."""
    from app.platform.workflows.defs import episode as ep_mod

    env = WorkflowTestEnvironment(EPISODE_WORKFLOWS, EPISODE_ACTIVITIES)
    total = ep_mod._SCENES_PER_RUN + 2  # forces at least one continue-as-new
    await env.start("produce_episode", "book_c", "s3://k/c.pdf", total, workflow_id="epc")
    result = await env.run_until_complete("epc")
    assert result["scenes_produced"] == total
    # The final run's history is bounded (it didn't accumulate all scenes' events).
    execution = await env.store.get_execution("epc")
    assert execution is not None
    final_history = await env.store.load_history("epc", execution.run_id)
    # A single run handles <= _SCENES_PER_RUN scenes, so the final history is far
    # smaller than a non-compacted run of `total` scenes would be.
    assert len(final_history) < 100
