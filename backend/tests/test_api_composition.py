"""Composition-root tests — the DI seams are satisfied with the REAL services.

Proves the single wiring point: ``MemoryTools`` is built with the real
:class:`RedisRenderEnqueuer` (so ``shot.render`` enqueues to the real Redis
queue) and the real :class:`Adapter` (so ``shot.plan`` runs the real planner).
"""

from __future__ import annotations

from app.agents.adapter import Adapter
from app.composition import Container
from app.db.repositories.beat import BeatRepo
from app.db.repositories.book import BookRepo
from app.db.repositories.scene import SceneRepo
from app.mcp import schemas
from app.queue.enqueuer import RedisRenderEnqueuer


def test_tools_are_wired_with_real_seams(container: Container) -> None:
    tools = container.build_tools()
    # RenderEnqueuer seam -> the real Redis priority-queue enqueuer.
    assert isinstance(tools._enqueuer, RedisRenderEnqueuer)
    # ShotPlanner seam -> the real Adapter.
    assert isinstance(tools._planner, Adapter)


async def test_shot_plan_runs_the_real_adapter(container: Container) -> None:
    async with container.session_factory() as session:
        book = await BookRepo(session).create(title="Plan Tale")
        await SceneRepo(session).create(
            book_id=book.id, scene_index=1, page_start=1, page_end=2, scene_id="scene_z"
        )
        await BeatRepo(session).create(
            book_id=book.id,
            scene_id="scene_z",
            beat_index=1,
            summary="the hero crosses the bridge",
            entities=[],
            described_visuals="a hero on a bridge",
            beat_id="beat_z1",
        )
    tools = container.build_tools()
    out = await tools.shot_plan(schemas.ShotPlanInput(scene_id="scene_z"))
    assert len(out.shots) >= 1
    assert out.shots[0].scene_id == "scene_z"


async def test_shot_render_enqueues_via_real_redis_queue(container: Container) -> None:
    async with container.session_factory() as session:
        book = await BookRepo(session).create(title="Render Tale")
        book_id = book.id

    tools = container.build_tools()
    out = await tools.shot_render(
        schemas.ShotRenderInput(
            book_id=book_id,
            beat_id="beat_q",
            scene_id="scene_q",
            render_mode="reference_to_video",
            seed=3,
            reference_image_ids=["char_hero@v1"],
            target_duration_s=5.0,
            priority="committed",
        )
    )
    assert out.status == "enqueued"
    assert out.job_id
    # The real RedisRenderEnqueuer wrote the job into the real queue (idempotency index).
    assert await container.queue.lookup(out.shot_hash) == out.job_id


def test_inference_router_seam_builds_network_free(container: Container) -> None:
    """The additive inference-router seam builds over the crew model stack with a
    network-free EchoBackend (no DashScope call, no credit spent)."""
    from app.inference.router import MultiModelRouter

    router = container.inference_router
    assert isinstance(router, MultiModelRouter)
    # One per-model router for each wired crew model (orchestration/high-volume/vl).
    assert set(router.models) == {
        container.settings.chat_model_max,
        container.settings.chat_model_plus,
        container.settings.vl_model,
    }
    # Cached: the property returns the same instance on re-access.
    assert container.inference_router is router
