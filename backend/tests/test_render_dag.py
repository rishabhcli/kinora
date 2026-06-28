"""The render-graph DAG for parallel, dependency-ordered shots (kinora.md §9.3/§9.6).

Topological ordering, deterministic ready-batches with a concurrency cap,
continuation-chain dependencies, cycle/dangling detection, blocked-on-degrade
handling, and end-to-end graph execution with a fake runner. No DB/ffmpeg.
"""

from __future__ import annotations

import pytest

from app.agents.contracts import RenderMode
from app.db.models.enums import ShotStatus
from app.render.dag import (
    CycleError,
    DanglingDependencyError,
    NodeState,
    RenderGraph,
    build_scene_graph,
    run_graph,
)


def _independent_graph(n: int) -> RenderGraph:
    graph = RenderGraph()
    for i in range(n):
        graph.add_shot(f"shot_{i}", order=i)
    return graph


def test_independent_shots_all_ready_in_one_batch() -> None:
    graph = _independent_graph(5)
    batch = graph.ready_batch(max_parallel=8)
    assert {n.shot_id for n in batch} == {f"shot_{i}" for i in range(5)}
    assert all(n.state is NodeState.RUNNING for n in batch)


def test_concurrency_cap_limits_batch_size_deterministically() -> None:
    graph = _independent_graph(5)
    batch = graph.ready_batch(max_parallel=2)
    # Deterministic: lowest order first.
    assert [n.shot_id for n in batch] == ["shot_0", "shot_1"]


def test_continuation_chain_orders_dependent_shots() -> None:
    graph = RenderGraph()
    graph.add_shot("a", render_mode=RenderMode.REFERENCE_TO_VIDEO, order=0)
    graph.add_shot("b", render_mode=RenderMode.VIDEO_CONTINUATION, order=1)
    graph.add_shot("c", render_mode=RenderMode.VIDEO_CONTINUATION, order=2)
    graph.link_continuation_chain(["a", "b", "c"])
    assert graph.nodes["b"].depends_on == {"a"}
    assert graph.nodes["c"].depends_on == {"b"}

    # Only 'a' is ready initially (b/c wait on their predecessors).
    first = graph.ready_batch(max_parallel=8)
    assert [n.shot_id for n in first] == ["a"]
    graph.mark_done("a", ShotStatus.ACCEPTED)
    second = graph.ready_batch(max_parallel=8)
    assert [n.shot_id for n in second] == ["b"]
    graph.mark_done("b", ShotStatus.ACCEPTED)
    third = graph.ready_batch(max_parallel=8)
    assert [n.shot_id for n in third] == ["c"]


def test_non_continuation_shots_are_not_chained() -> None:
    graph = RenderGraph()
    graph.add_shot("a", render_mode=RenderMode.REFERENCE_TO_VIDEO, order=0)
    graph.add_shot("b", render_mode=RenderMode.TEXT_TO_VIDEO, order=1)
    graph.link_continuation_chain(["a", "b"])
    assert graph.nodes["b"].depends_on == set()  # b is independent → fans out
    batch = graph.ready_batch(max_parallel=8)
    assert {n.shot_id for n in batch} == {"a", "b"}


def test_topological_order_is_deterministic() -> None:
    graph = RenderGraph()
    graph.add_shot("a", order=0)
    graph.add_shot("b", render_mode=RenderMode.VIDEO_CONTINUATION, depends_on=["a"], order=1)
    graph.add_shot("c", order=2)
    order = graph.topological_order()
    assert order.index("a") < order.index("b")  # dependency respected
    assert "c" in order


def test_dangling_dependency_detected() -> None:
    graph = RenderGraph()
    graph.add_shot("b", depends_on=["missing"])
    with pytest.raises(DanglingDependencyError):
        graph.validate()


def test_cycle_detected() -> None:
    graph = RenderGraph()
    graph.add_shot("a", depends_on=["b"])
    graph.add_shot("b", depends_on=["a"])
    with pytest.raises(CycleError):
        graph.validate()


def test_continuation_blocks_when_predecessor_degraded() -> None:
    graph = RenderGraph()
    graph.add_shot("a", render_mode=RenderMode.REFERENCE_TO_VIDEO, order=0)
    graph.add_shot("b", render_mode=RenderMode.VIDEO_CONTINUATION, depends_on=["a"], order=1)
    # 'a' degraded → no accepted endpoint for b to continue from.
    graph.mark_done("a", ShotStatus.DEGRADED)
    batch = graph.ready_batch(max_parallel=8)
    assert batch == []  # b can't continuation-render
    assert [n.shot_id for n in graph.blocked] == ["b"]
    assert graph.is_complete


async def test_run_graph_executes_in_dependency_order_with_parallelism() -> None:
    graph = build_scene_graph(
        [
            {"shot_id": "a", "render_mode": "reference_to_video"},
            {"shot_id": "b", "render_mode": "video_continuation"},
            {"shot_id": "x", "render_mode": "text_to_video"},
            {"shot_id": "y", "render_mode": "text_to_video"},
        ]
    )
    order_seen: list[str] = []

    async def runner(shot_id: str) -> ShotStatus:
        order_seen.append(shot_id)
        return ShotStatus.ACCEPTED

    report = await run_graph(graph, runner, max_parallel=4)
    assert set(report.completed) == {"a", "b", "x", "y"}
    assert report.blocked == []
    # 'b' (continuation) renders strictly after 'a'.
    assert order_seen.index("a") < order_seen.index("b")
    # The first batch fanned out the independents (a, x, y) in parallel.
    assert report.batches[0] == ["a", "x", "y"]
    assert report.max_parallelism == 3
    assert report.batch_count == 2


async def test_run_graph_reports_blocked_continuation() -> None:
    graph = build_scene_graph(
        [
            {"shot_id": "a", "render_mode": "reference_to_video"},
            {"shot_id": "b", "render_mode": "video_continuation"},
        ]
    )

    async def runner(shot_id: str) -> ShotStatus:
        return ShotStatus.DEGRADED if shot_id == "a" else ShotStatus.ACCEPTED

    report = await run_graph(graph, runner, max_parallel=4)
    assert report.completed == ["a"]
    assert report.blocked == ["b"]  # couldn't continuation-render off a degrade


def test_build_scene_graph_chains_continuations() -> None:
    graph = build_scene_graph(
        [
            {"shot_id": "s0", "render_mode": "reference_to_video", "scene_id": "scene_1"},
            {"shot_id": "s1", "render_mode": "video_continuation", "scene_id": "scene_1"},
        ]
    )
    assert graph.nodes["s1"].depends_on == {"s0"}
    assert graph.nodes["s1"].scene_id == "scene_1"
    assert len(graph) == 2
