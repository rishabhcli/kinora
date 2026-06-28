"""MCP tool surface — protocol round-trip, cache/budget flow, and Qwen skills (§8.3, §14).

Exercises the §8.3 tools three ways: through the official MCP protocol (an
in-memory client↔server session), directly through the typed tool layer, and
through the Qwen function-call skill dispatcher. ``shot.render`` is driven over a
real cache + a real persistent budget with a **fake** RenderEnqueuer — a
legitimate test double for an injected seam. A real-embedding round-trip is
gated behind ``KINORA_LIVE_TESTS``.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
import pytest_asyncio
from mcp.shared.memory import create_connected_server_and_client_session
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.db import models  # noqa: F401  (register tables on Base.metadata)
from app.db.base import Base
from app.db.models.enums import EntityType, RenderPriority
from app.db.repositories.beat import BeatRepo
from app.db.repositories.book import BookRepo
from app.db.repositories.budget import BudgetRepo
from app.db.repositories.scene import SceneRepo
from app.db.repositories.shot import ShotCacheRepo, ShotRepo
from app.mcp import schemas
from app.mcp.server import build_server
from app.mcp.skills import QwenSkillDispatcher
from app.mcp.tools import TOOL_DEFS, MemoryTools, SessionFactory
from app.memory.budget_service import BudgetLimits, BudgetService
from app.memory.canon_service import CanonService
from app.memory.episodic_service import EpisodicService
from app.memory.interfaces import NotWired, ShotSpec

_DB_URL = os.environ.get("KINORA_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not _DB_URL, reason="KINORA_TEST_DATABASE_URL not set; skipping DB integration tests"
)

_DIM = 1152
_LIMITS = BudgetLimits(
    ceiling_video_s=100.0,
    per_session_s=50.0,
    per_scene_s=30.0,
    low_floor_s=10.0,
    live_video=False,
)


class FakeEmbedder:
    """Deterministic one-hot embedder (test double for ``Embedder``)."""

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        return [self._one_hot(t.encode("utf-8")) for t in texts]

    async def embed_images(self, images: list[bytes]) -> list[list[float]]:
        return [self._one_hot(b) for b in images]

    @staticmethod
    def _one_hot(data: bytes) -> list[float]:
        axis = int.from_bytes(hashlib.sha1(data).digest()[:4], "big") % _DIM
        vector = [0.0] * _DIM
        vector[axis] = 1.0
        return vector


class FakeEnqueuer:
    """Captures enqueue calls (a test double for the injected ``RenderEnqueuer``)."""

    def __init__(self) -> None:
        self.calls: list[tuple[ShotSpec, RenderPriority, str | None]] = []

    async def enqueue(
        self, shot_spec: ShotSpec, priority: RenderPriority, cancel_token: str | None = None
    ) -> str:
        self.calls.append((shot_spec, priority, cancel_token))
        return "job_test_1"


class FakePlanner:
    """A trivial injected ``ShotPlanner`` (the real Adapter arrives in a later phase)."""

    async def plan_scene(self, scene_id: str) -> list[ShotSpec]:
        return [ShotSpec(book_id="b", beat_id="beat_x", scene_id=scene_id)]


@pytest_asyncio.fixture
async def maker() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    assert _DB_URL is not None
    engine = create_async_engine(_DB_URL, poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.run_sync(Base.metadata.create_all)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield sessionmaker
    finally:
        await engine.dispose()


def _factory(sessionmaker: async_sessionmaker[AsyncSession]) -> SessionFactory:
    """A committing unit-of-work factory bound to the test engine."""

    @asynccontextmanager
    async def factory() -> AsyncIterator[AsyncSession]:
        async with sessionmaker() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    return factory


def _tools(
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    enqueuer: FakeEnqueuer,
    embedder: FakeEmbedder | None = None,
    planner: FakePlanner | None = None,
) -> MemoryTools:
    return MemoryTools(
        embedder=embedder or FakeEmbedder(),
        session_factory=_factory(sessionmaker),
        limits=_LIMITS,
        enqueuer=enqueuer,
        planner=planner or FakePlanner(),
    )


async def _seed_book(sessionmaker: async_sessionmaker[AsyncSession]) -> tuple[str, str, str]:
    """Seed a tiny book; returns ``(book_id, scene_id, beat_id)`` (ids unique per call)."""
    factory = _factory(sessionmaker)
    async with factory() as session:
        book = await BookRepo(session).create(title="MCP Tale")
        suffix = book.id[:12]
        scene_id = f"scene_{suffix}"
        beat_id = f"beat_{suffix}"
        await SceneRepo(session).create(
            book_id=book.id, scene_index=1, page_start=1, page_end=2, scene_id=scene_id
        )
        canon = CanonService(session, embedder=FakeEmbedder())
        await canon.upsert_entity(
            book_id=book.id,
            entity_key="char_hero",
            entity_type=EntityType.CHARACTER,
            name="Hero",
            valid_from_beat=1,
        )
        await BeatRepo(session).create(
            book_id=book.id,
            scene_id=scene_id,
            beat_index=1,
            summary="hero scene",
            entities=["char_hero"],
            described_visuals="a hero in a hall",
            beat_id=beat_id,
        )
        return book.id, scene_id, beat_id


async def test_mcp_protocol_roundtrip(maker: async_sessionmaker[AsyncSession]) -> None:
    book_id, _scene_id, beat_id = await _seed_book(maker)
    tools = _tools(maker, enqueuer=FakeEnqueuer())
    server = build_server(tools)

    async with create_connected_server_and_client_session(server) as client:
        listed = await client.list_tools()
        names = {tool.name for tool in listed.tools}
        assert {"canon.query", "budget.remaining", "shot.render"} <= names
        # Bitemporal canon engine (§8) added 12 tools through the same dispatch path.
        assert {"canon.assert_fact", "canon.fork", "canon.merge", "canon.view"} <= names
        assert {"canon.compact", "canon.vault"} <= names
        assert len(names) == len(TOOL_DEFS) == 27

        result = await client.call_tool(
            "canon.query", {"book_id": book_id, "beat_id": beat_id}
        )
        assert result.isError is False
        slice_data = result.structuredContent
        assert slice_data is not None
        assert any(c["entity_key"] == "char_hero" for c in slice_data["characters"])

        remaining = await client.call_tool("budget.remaining", {})
        budget_data = remaining.structuredContent
        assert budget_data is not None
        assert budget_data["ceiling_video_s"] == 100.0
        assert budget_data["can_render_live"] is False


async def test_shot_render_miss_enqueues_without_pre_reserving_budget(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    """shot.render must NOT pre-reserve budget (the §8.7 leak/double-count fix).

    On a miss it only enqueues; the RenderPipeline owns the single authoritative
    reserve → commit/release lifecycle. So the ledger is unchanged right after the
    enqueue (no leaked phantom reservation), and when the pipeline later reserves +
    commits the real seconds the budget drops by 5s *exactly once* — never 10s.
    """
    book_id, scene_id, beat_id = await _seed_book(maker)
    enqueuer = FakeEnqueuer()
    tools = _tools(maker, enqueuer=enqueuer)

    baseline = (await tools.budget_remaining(schemas.BudgetRemainingInput())).remaining_video_s

    render_args = schemas.ShotRenderInput(
        book_id=book_id,
        beat_id=beat_id,
        scene_id=scene_id,
        render_mode="reference_to_video",
        seed=11,
        reference_image_ids=["char_hero@v1"],
        target_duration_s=5.0,
        priority="committed",
    )

    miss = await tools.shot_render(render_args)
    assert miss.status == "enqueued"
    assert miss.cached is False
    assert miss.job_id == "job_test_1"
    assert miss.reservation_id is None  # shot.render reserves nothing
    assert len(enqueuer.calls) == 1
    spec, priority, _token = enqueuer.calls[0]
    assert spec.shot_hash == miss.shot_hash
    assert spec.reference_set_hash == miss.reference_set_hash
    assert priority == RenderPriority.COMMITTED

    # No leak: the budget ledger is untouched by the enqueue.
    after_miss = (await tools.budget_remaining(schemas.BudgetRemainingInput())).remaining_video_s
    assert after_miss == pytest.approx(baseline)

    # The single authoritative lifecycle (the pipeline) charges the real seconds ONCE:
    # reserve + commit on the same reservation nets to 5s used (not 10s).
    async with _factory(maker)() as session:
        budget = BudgetService(repo=BudgetRepo(session), limits=_LIMITS)
        reservation = await budget.reserve(5.0, scene_id=scene_id, book_id=book_id)
        await budget.commit(reservation, 5.0)

    after_render = (await tools.budget_remaining(schemas.BudgetRemainingInput())).remaining_video_s
    assert after_render == pytest.approx(baseline - 5.0)


async def test_shot_render_cache_hit_serves_zero_video_seconds(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    book_id, scene_id, beat_id = await _seed_book(maker)
    enqueuer = FakeEnqueuer()
    tools = _tools(maker, enqueuer=enqueuer)

    render_args = schemas.ShotRenderInput(
        book_id=book_id,
        beat_id=beat_id,
        scene_id=scene_id,
        render_mode="reference_to_video",
        seed=11,
        reference_image_ids=["char_hero@v1"],
        target_duration_s=5.0,
        priority="committed",
    )
    miss = await tools.shot_render(render_args)
    assert miss.status == "enqueued"

    # Populate the cache for that shot_hash → the next identical render is a hit.
    async with _factory(maker)() as session:
        await ShotCacheRepo(session).put(
            shot_hash=miss.shot_hash,
            book_id=book_id,
            clip_key="clips/hit.mp4",
            video_seconds=5.0,
        )

    baseline = (await tools.budget_remaining(schemas.BudgetRemainingInput())).remaining_video_s
    hit = await tools.shot_render(render_args)
    assert hit.status == "cache_hit"
    assert hit.cached is True
    assert hit.video_seconds == 0.0
    assert hit.shot_hash == miss.shot_hash
    assert len(enqueuer.calls) == 1  # no second enqueue

    after_hit = (await tools.budget_remaining(schemas.BudgetRemainingInput())).remaining_video_s
    assert after_hit == pytest.approx(baseline)  # a hit reserves nothing


async def test_shot_plan_seam(maker: async_sessionmaker[AsyncSession]) -> None:
    tools = _tools(maker, enqueuer=FakeEnqueuer(), planner=FakePlanner())
    planned = await tools.shot_plan(schemas.ShotPlanInput(scene_id="scene_m"))
    assert len(planned.shots) == 1
    assert planned.shots[0].scene_id == "scene_m"

    # The default planner is the NotWired DI seam (Phase 8 injects the real one).
    bare = MemoryTools(
        embedder=FakeEmbedder(),
        session_factory=_factory(maker),
        limits=BudgetLimits(
            ceiling_video_s=100.0,
            per_session_s=50.0,
            per_scene_s=30.0,
            low_floor_s=10.0,
            live_video=False,
        ),
    )
    with pytest.raises(NotWired):
        await bare.shot_plan(schemas.ShotPlanInput(scene_id="scene_m"))


async def test_qwen_skill_definitions_and_dispatch(
    maker: async_sessionmaker[AsyncSession],
) -> None:
    book_id, _scene_id, beat_id = await _seed_book(maker)
    tools = _tools(maker, enqueuer=FakeEnqueuer())
    dispatcher = QwenSkillDispatcher(tools)

    featured = dispatcher.definitions(featured_only=True)
    function_names = {d["function"]["name"] for d in featured}
    assert function_names == {"canon_query", "shot_render"}
    for definition in featured:
        assert definition["type"] == "function"
        assert "properties" in definition["function"]["parameters"]
    assert len(dispatcher.definitions()) == len(TOOL_DEFS)

    # Dispatch via the function-call name with a JSON-string argument (as Qwen emits).
    canon_result = await dispatcher.dispatch(
        "canon_query", json.dumps({"book_id": book_id, "beat_id": beat_id})
    )
    assert any(c["entity_key"] == "char_hero" for c in canon_result["characters"])

    # Dispatch via a dict argument, accepting the dotted name too.
    budget_result = await dispatcher.dispatch("budget.remaining", {})
    assert budget_result["ceiling_video_s"] == 100.0


@pytest.mark.skipif(
    not os.environ.get("KINORA_LIVE_TESTS"),
    reason="KINORA_LIVE_TESTS not set; skipping real-embedding test",
)
async def test_real_embeddings_roundtrip(maker: async_sessionmaker[AsyncSession]) -> None:
    from app.providers import create_providers

    providers = create_providers()
    embedder = providers.embeddings
    try:
        factory = _factory(maker)
        async with factory() as session:
            book = await BookRepo(session).create(title="Live")
            await SceneRepo(session).create(
                book_id=book.id, scene_index=1, page_start=1, page_end=1, scene_id="scene_l"
            )
            canon = CanonService(session, embedder=embedder)
            await canon.upsert_entity(
                book_id=book.id,
                entity_key="char_x",
                entity_type=EntityType.CHARACTER,
                name="X",
                valid_from_beat=1,
            )
            await BeatRepo(session).create(
                book_id=book.id,
                scene_id="scene_l",
                beat_index=1,
                summary="s",
                entities=["char_x"],
                described_visuals="a knight in a forest",
                beat_id="beat_l1",
            )
            await EpisodicService(shots=ShotRepo(session), embedder=embedder).log(
                book_id=book.id,
                beat_id="beat_l1",
                scene_id="scene_l",
                described_visuals_text="a knight in a forest",
                output={"clip_key": "clips/x.mp4"},
                qa={"verdict": "pass"},
            )
            book_id = book.id

        vectors = await embedder.embed_texts(["a knight in a forest"])
        assert len(vectors[0]) == 1152

        tools = MemoryTools(
            embedder=embedder,
            session_factory=_factory(maker),
            limits=BudgetLimits(
                ceiling_video_s=100.0,
                per_session_s=50.0,
                per_scene_s=30.0,
                low_floor_s=10.0,
                live_video=False,
            ),
            enqueuer=FakeEnqueuer(),
        )
        result = await tools.canon_query(
            schemas.CanonQueryInput(book_id=book_id, beat_id="beat_l1")
        )
        assert any(shot.beat_id == "beat_l1" for shot in result.episodic)
    finally:
        await providers.aclose()
