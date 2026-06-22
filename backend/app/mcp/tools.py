"""MCP tool implementations + the SDK-agnostic dispatcher (kinora.md §8.3).

``MemoryTools`` is the single place the tool surface is implemented: one method
per tool, each opening a unit-of-work session, building the relevant memory
service(s) over it, and returning a typed pydantic result. ``dispatch`` validates
raw arguments into the tool's input model and routes to the method — both the MCP
server (:mod:`app.mcp.server`) and the Qwen skill dispatcher (:mod:`app.mcp.skills`)
go through it, so there is exactly one execution path to test.

The render/queue and the Adapter are **injected** seams (``RenderEnqueuer`` /
``ShotPlanner``); Phase 4 never implements them (see :class:`app.memory.interfaces.NotWired`).
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass

import anyio
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.enums import EntityType, RenderPriority, ShotStatus
from app.db.repositories.budget import BudgetRepo
from app.db.repositories.pref import PrefsRepo
from app.db.repositories.render_job import RenderJobRepo
from app.db.repositories.shot import ShotCacheRepo, ShotRepo
from app.db.session import get_session
from app.mcp import schemas
from app.memory.budget_service import BudgetExceeded, BudgetLimits, BudgetService
from app.memory.cache_service import CacheService
from app.memory.canon_service import CanonService
from app.memory.episodic_service import EpisodicService
from app.memory.interfaces import (
    BlobStore,
    Embedder,
    NotWiredRenderEnqueuer,
    NotWiredShotPlanner,
    RenderEnqueuer,
    ShotPlanner,
    ShotSpec,
)
from app.memory.prefs_service import PreferencePrior, PreferencePriors, PrefsService

SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]


@dataclass(frozen=True, slots=True)
class ToolDef:
    """One registered MCP tool: its name, description, and input model."""

    name: str
    description: str
    input_model: type[BaseModel]
    handler: str


class MemoryTools:
    """The §8.3 tool surface, delegating to the memory services over a session."""

    def __init__(
        self,
        *,
        embedder: Embedder,
        session_factory: SessionFactory = get_session,
        blob_store: BlobStore | None = None,
        limits: BudgetLimits,
        enqueuer: RenderEnqueuer | None = None,
        planner: ShotPlanner | None = None,
        url_ttl: int = 3600,
    ) -> None:
        self._sf = session_factory
        self._embedder = embedder
        self._store = blob_store
        self._limits = limits
        self._enqueuer: RenderEnqueuer = enqueuer or NotWiredRenderEnqueuer()
        self._planner: ShotPlanner = planner or NotWiredShotPlanner()
        self._ttl = url_ttl

    # --- service builders --------------------------------------------------- #

    def _canon(self, session: AsyncSession) -> CanonService:
        return CanonService(
            session, embedder=self._embedder, blob_store=self._store, url_ttl=self._ttl
        )

    def _episodic(self, session: AsyncSession) -> EpisodicService:
        return EpisodicService(
            shots=ShotRepo(session),
            embedder=self._embedder,
            blob_store=self._store,
            url_ttl=self._ttl,
        )

    def _cache(self, session: AsyncSession) -> CacheService:
        return CacheService(
            cache=ShotCacheRepo(session), blob_store=self._store, url_ttl=self._ttl
        )

    def _budget(self, session: AsyncSession) -> BudgetService:
        return BudgetService(repo=BudgetRepo(session), limits=self._limits)

    def _prefs(self, session: AsyncSession) -> PrefsService:
        return PrefsService(prefs=PrefsRepo(session))

    # --- canon.* ------------------------------------------------------------ #

    async def canon_query(self, inp: schemas.CanonQueryInput) -> schemas.CanonSlice:
        async with self._sf() as session:
            return await self._canon(session).query(
                inp.book_id, inp.beat_id, inp.kinds, episodic_k=inp.episodic_k
            )

    async def canon_get_entity(
        self, inp: schemas.CanonGetEntityInput
    ) -> schemas.CanonGetEntityOutput:
        async with self._sf() as session:
            entity = await self._canon(session).get_entity(
                inp.book_id, inp.entity_key, inp.at_beat
            )
        return schemas.CanonGetEntityOutput(found=entity is not None, entity=entity)

    async def canon_upsert_entity(
        self, inp: schemas.CanonUpsertEntityInput
    ) -> schemas.CanonUpsertEntityOutput:
        entity_type = EntityType(inp.type)
        async with self._sf() as session:
            version = await self._canon(session).upsert_entity(
                book_id=inp.book_id,
                entity_key=inp.entity_key,
                entity_type=entity_type,
                name=inp.name,
                valid_from_beat=inp.valid_from_beat,
                aliases=inp.aliases,
                description=inp.description,
                appearance=inp.appearance,
                voice=inp.voice,
                style_tokens=inp.style_tokens,
                first_appearance=inp.first_appearance,
                entity_id=inp.entity_id,
            )
        return schemas.CanonUpsertEntityOutput(entity_key=inp.entity_key, version=version)

    async def canon_assert_state(
        self, inp: schemas.CanonAssertStateInput
    ) -> schemas.CanonAssertStateOutput:
        async with self._sf() as session:
            state_id = await self._canon(session).assert_state(
                book_id=inp.book_id,
                subject_entity_key=inp.subject_entity_key,
                predicate=inp.predicate,
                object_value=inp.object_value,
                valid_from_beat=inp.valid_from_beat,
                source_span=inp.source_span,
                state_id=inp.state_id,
            )
        return schemas.CanonAssertStateOutput(state_id=state_id)

    async def canon_retire_state(
        self, inp: schemas.CanonRetireStateInput
    ) -> schemas.CanonRetireStateOutput:
        async with self._sf() as session:
            await self._canon(session).retire_state(inp.state_id, inp.valid_to_beat)
        return schemas.CanonRetireStateOutput(
            state_id=inp.state_id, valid_to_beat=inp.valid_to_beat
        )

    # --- shot.* ------------------------------------------------------------- #

    async def shot_plan(self, inp: schemas.ShotPlanInput) -> schemas.ShotPlanOutput:
        shots = await self._planner.plan_scene(inp.scene_id)
        return schemas.ShotPlanOutput(scene_id=inp.scene_id, shots=list(shots))

    async def shot_render(self, inp: schemas.ShotRenderInput) -> schemas.ShotRenderOutput:
        async with self._sf() as session:
            cache = self._cache(session)
            look = await cache.check_or_miss(
                book_id=inp.book_id,
                beat_id=inp.beat_id,
                canon_version_at_render=inp.canon_version_at_render,
                render_mode=inp.render_mode,
                seed=inp.seed,
                reference_image_ids=inp.reference_image_ids,
            )
            if look.hit:
                # Cache hit: serve the cached clip, spend zero video-seconds (§8.7).
                return schemas.ShotRenderOutput(
                    status="cache_hit",
                    cached=True,
                    shot_hash=look.shot_hash,
                    reference_set_hash=look.reference_set_hash,
                    clip_url=look.clip_url,
                    last_frame_url=look.last_frame_url,
                    video_seconds=0.0,
                )

            budget = self._budget(session)
            try:
                reservation = await budget.reserve(
                    inp.target_duration_s,
                    session_id=inp.session_id,
                    scene_id=inp.scene_id,
                    book_id=inp.book_id,
                )
            except BudgetExceeded as exc:
                remaining = await budget.remaining()
                return schemas.ShotRenderOutput(
                    status="budget_exceeded",
                    cached=False,
                    shot_hash=look.shot_hash,
                    reference_set_hash=look.reference_set_hash,
                    video_seconds=0.0,
                    remaining_video_s=remaining,
                    reason=str(exc),
                )

            spec = ShotSpec(
                book_id=inp.book_id,
                beat_id=inp.beat_id,
                scene_id=inp.scene_id,
                shot_id=inp.shot_id,
                render_mode=inp.render_mode,
                prompt=inp.prompt,
                negative_prompt=inp.negative_prompt,
                reference_image_ids=inp.reference_image_ids,
                camera=inp.camera,
                seed=inp.seed,
                target_duration_s=inp.target_duration_s,
                canon_version_at_render=inp.canon_version_at_render,
                reference_set_hash=look.reference_set_hash,
                shot_hash=look.shot_hash,
                end_frame_ref=inp.end_frame_ref,
            )
            job_id = await self._enqueuer.enqueue(
                spec, RenderPriority(inp.priority), inp.cancel_token
            )
            return schemas.ShotRenderOutput(
                status="enqueued",
                cached=False,
                shot_hash=look.shot_hash,
                reference_set_hash=look.reference_set_hash,
                reservation_id=reservation.id,
                job_id=job_id,
                video_seconds=inp.target_duration_s,
            )

    async def shot_status(self, inp: schemas.ShotStatusInput) -> schemas.ShotStatusOutput:
        async with self._sf() as session:
            job = await RenderJobRepo(session).get(inp.job_id)
            if job is None:
                return schemas.ShotStatusOutput(found=False, job_id=inp.job_id)
            return schemas.ShotStatusOutput(
                found=True,
                job_id=job.id,
                status=job.status.value,
                attempts=job.attempts,
                provider_task_id=job.provider_task_id,
                error=job.error,
                shot_id=job.shot_id,
                shot_hash=job.shot_hash,
            )

    async def shot_result(self, inp: schemas.ShotResultInput) -> schemas.ShotResultOutput:
        async with self._sf() as session:
            shot = await ShotRepo(session).get(inp.shot_id)
            if shot is None:
                return schemas.ShotResultOutput(found=False, shot_id=inp.shot_id)
            output = shot.output or {}
            return schemas.ShotResultOutput(
                found=True,
                shot_id=shot.id,
                status=shot.status.value,
                output=shot.output,
                narration=shot.narration,
                qa=shot.qa,
                duration_s=shot.duration_s,
                clip_url=self._presign(output.get("clip_key")),
                last_frame_url=self._presign(output.get("last_frame_key")),
            )

    # --- episodic.* --------------------------------------------------------- #

    async def episodic_search(
        self, inp: schemas.EpisodicSearchInput
    ) -> schemas.EpisodicSearchOutput:
        image_bytes = await self._maybe_get_bytes(inp.query_image_key)
        async with self._sf() as session:
            shots = await self._episodic(session).search(
                inp.book_id,
                query_embedding=inp.query_embedding,
                query_image_bytes=image_bytes,
                described_visuals_text=inp.described_visuals_text,
                k=inp.k,
                filters=inp.filters,
            )
        return schemas.EpisodicSearchOutput(shots=shots)

    async def episodic_log(self, inp: schemas.EpisodicLogInput) -> schemas.EpisodicLogOutput:
        last_frame_bytes = await self._maybe_get_bytes(inp.last_frame_key)
        keyframe_bytes = await self._maybe_get_bytes(inp.keyframe_key)
        async with self._sf() as session:
            shot = await self._episodic(session).log(
                book_id=inp.book_id,
                status=ShotStatus(inp.status),
                shot_id=inp.shot_id,
                beat_id=inp.beat_id,
                scene_id=inp.scene_id,
                source_span=inp.source_span,
                render_mode=inp.render_mode,
                prompt=inp.prompt,
                negative_prompt=inp.negative_prompt,
                seed=inp.seed,
                reference_set_hash=inp.reference_set_hash,
                reference_image_ids=inp.reference_image_ids,
                duration_s=inp.duration_s,
                output=inp.output,
                narration=inp.narration,
                qa=inp.qa,
                cost=inp.cost,
                canon_version_at_render=inp.canon_version_at_render,
                shot_hash=inp.shot_hash,
                last_frame_bytes=last_frame_bytes,
                keyframe_bytes=keyframe_bytes,
                described_visuals_text=inp.described_visuals_text,
            )
        return schemas.EpisodicLogOutput(shot_id=shot.id, status=shot.status.value)

    # --- budget.* ----------------------------------------------------------- #

    async def budget_reserve(self, inp: schemas.BudgetReserveInput) -> schemas.BudgetReserveOutput:
        async with self._sf() as session:
            budget = self._budget(session)
            try:
                reservation = await budget.reserve(
                    inp.video_seconds,
                    session_id=inp.session_id,
                    scene_id=inp.scene_id,
                    book_id=inp.book_id,
                )
            except BudgetExceeded as exc:
                remaining = await budget.remaining()
                return schemas.BudgetReserveOutput(
                    reserved=False,
                    video_seconds=inp.video_seconds,
                    remaining_video_s=remaining,
                    reason=str(exc),
                    scope=exc.scope,
                )
            remaining = await budget.remaining()
            return schemas.BudgetReserveOutput(
                reserved=True,
                video_seconds=reservation.video_seconds,
                remaining_video_s=remaining,
                reservation_id=reservation.id,
            )

    async def budget_remaining(
        self, inp: schemas.BudgetRemainingInput
    ) -> schemas.BudgetRemainingOutput:
        async with self._sf() as session:
            budget = self._budget(session)
            remaining = await budget.remaining()
            is_low = await budget.is_low()
            return schemas.BudgetRemainingOutput(
                remaining_video_s=remaining,
                ceiling_video_s=self._limits.ceiling_video_s,
                is_low=is_low,
                can_render_live=budget.can_render_live(),
            )

    # --- prefs.* ------------------------------------------------------------ #

    async def prefs_get(self, inp: schemas.PrefsGetInput) -> PreferencePriors:
        async with self._sf() as session:
            return await self._prefs(session).get(user_id=inp.user_id, book_id=inp.book_id)

    async def prefs_upsert(self, inp: schemas.PrefsUpsertInput) -> PreferencePrior:
        async with self._sf() as session:
            return await self._prefs(session).upsert(
                kind=inp.kind,
                value=inp.value,
                user_id=inp.user_id,
                book_id=inp.book_id,
                weight_delta=inp.weight_delta,
            )

    # --- dispatch ----------------------------------------------------------- #

    async def dispatch(self, name: str, arguments: dict[str, object]) -> BaseModel:
        """Validate ``arguments`` into the tool's input model and run the handler."""
        defn = TOOLS_BY_NAME.get(name)
        if defn is None:
            raise ValueError(f"unknown tool: {name}")
        model = defn.input_model.model_validate(arguments)
        handler = getattr(self, defn.handler)
        return await handler(model)

    # --- helpers ------------------------------------------------------------ #

    async def _maybe_get_bytes(self, key: str | None) -> bytes | None:
        if key is None or self._store is None:
            return None
        store = self._store
        return await anyio.to_thread.run_sync(store.get_bytes, key)

    def _presign(self, key: str | None) -> str | None:
        if key is None or self._store is None:
            return None
        return self._store.presigned_get_url(key, ttl=self._ttl)


#: The complete §8.3 tool surface. Order is the natural read order.
TOOL_DEFS: list[ToolDef] = [
    ToolDef(
        "canon.query",
        "Retrieval policy: return only the canon a beat needs — characters "
        "present (resolved at this beat's version), the active location, the "
        "scene's style tokens, active continuity facts, the previous endpoint "
        "frame, and top-k similar prior shots. Never the whole book.",
        schemas.CanonQueryInput,
        "canon_query",
    ),
    ToolDef(
        "canon.get_entity",
        "Resolve a versioned canon entity as of a beat (time-travel read).",
        schemas.CanonGetEntityInput,
        "canon_get_entity",
    ),
    ToolDef(
        "canon.upsert_entity",
        "Write a new version of a canon entity; embeds the locked reference "
        "image into the appearance vector when present.",
        schemas.CanonUpsertEntityInput,
        "canon_upsert_entity",
    ),
    ToolDef(
        "canon.assert_state",
        "Add a versioned continuity fact valid from a beat (open-ended).",
        schemas.CanonAssertStateInput,
        "canon_assert_state",
    ),
    ToolDef(
        "canon.retire_state",
        "Forgetting: close a continuity fact's validity interval so it drops "
        "out of active retrieval (history preserved for time-travel reads).",
        schemas.CanonRetireStateInput,
        "canon_retire_state",
    ),
    ToolDef(
        "shot.plan",
        "Decompose a scene into an ordered shot list (Adapter; injected).",
        schemas.ShotPlanInput,
        "shot_plan",
    ),
    ToolDef(
        "shot.render",
        "Render a shot: check the content-hash cache first (hit => cached clip "
        "at zero video-seconds); on a miss, reserve budget and enqueue the "
        "render (honours cache + budget).",
        schemas.ShotRenderInput,
        "shot_render",
    ),
    ToolDef(
        "shot.status",
        "Poll a render job's queue status.",
        schemas.ShotStatusInput,
        "shot_status",
    ),
    ToolDef(
        "shot.result",
        "Fetch a finished shot's output, narration, and QA (with signed URLs).",
        schemas.ShotResultInput,
        "shot_result",
    ),
    ToolDef(
        "episodic.search",
        "What worked before: nearest prior accepted shots for a similar beat.",
        schemas.EpisodicSearchInput,
        "episodic_search",
    ),
    ToolDef(
        "episodic.log",
        "Persist a shot + its QA and compute/store its retrieval embedding.",
        schemas.EpisodicLogInput,
        "episodic_log",
    ),
    ToolDef(
        "budget.reserve",
        "Earmark video-seconds before a render; refuses if a cap would break.",
        schemas.BudgetReserveInput,
        "budget_reserve",
    ),
    ToolDef(
        "budget.remaining",
        "Remaining video-seconds against the hard ceiling, plus the low/go-live "
        "flags.",
        schemas.BudgetRemainingInput,
        "budget_remaining",
    ),
    ToolDef(
        "prefs.get",
        "Read aggregated Director-preference priors for a user/book scope.",
        schemas.PrefsGetInput,
        "prefs_get",
    ),
    ToolDef(
        "prefs.upsert",
        "Nudge a Director-preference prior (pacing / palette / composition).",
        schemas.PrefsUpsertInput,
        "prefs_upsert",
    ),
]

#: Name -> definition for O(1) dispatch.
TOOLS_BY_NAME: dict[str, ToolDef] = {defn.name: defn for defn in TOOL_DEFS}


__all__ = ["MemoryTools", "SessionFactory", "TOOL_DEFS", "TOOLS_BY_NAME", "ToolDef"]
