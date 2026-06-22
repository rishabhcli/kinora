"""Per-shot Phase-B render orchestrator + the §9.7 state machine (§9.2, §9.5–§9.7).

This is the heart of "Phase B": given a promoted shot, it runs the §9.7 state
machine end-to-end —

    Promoted → CacheCheck → (cache hit → Accepted, 0 video-s)
                         → (miss → Rendering → QA → Accepted)
                                            → Repair → Rendering (retry ≤ 2)
                                                     → Conflict (§7.2)
                                                     → Degraded (Ken-Burns)

— wiring the real collaborators built in earlier phases: ``canon.query`` for the
slice, the Cinematographer for the spec, the budget guardrail + Generator for
the clip, the Critic's self-correcting repair loop (§9.5), the conflict
arbitration flow (§7.2), and — when the live gate is off, budget is low, or
retries are exhausted — the **real ffmpeg degradation ladder** (§4.4/§12.4).

Every collaborator is injected behind a narrow Protocol so the real services fit
and tests can supply light doubles for the heavy provider calls. The degradation
rung is a genuine product feature: it produces a real, playable Ken-Burns mp4
muxed with narration, never a fake placeholder.
"""

from __future__ import annotations

import math
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

import anyio
from pydantic import BaseModel, ConfigDict

from app.agents.contracts import (
    Beat as AgentBeat,
)
from app.agents.contracts import (
    ConflictObject,
    DirectorNote,
    QARecord,
    RepairAction,
    SourceSpan,
    Verdict,
)
from app.agents.contracts import (
    ShotSpec as AgentShotSpec,
)
from app.agents.generator import GeneratorOutput
from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.db.models.enums import ShotStatus
from app.memory.budget_service import BudgetExceeded, Reservation
from app.memory.interfaces import BlobStore, CanonSlice, Embedder
from app.observability import metrics
from app.providers.errors import LiveVideoDisabled, ProviderError
from app.providers.types import TtsResult
from app.render.conflict import ConflictResolution
from app.render.degrade import (
    DegradeRung,
    FfmpegError,
    audio_text_card,
    extract_frames,
    ken_burns_over_image,
)
from app.render.states import RenderState, ShotStateMachine
from app.render.sync_map import build_sync_segment
from app.storage.object_store import keys

logger = get_logger("app.render.pipeline")

#: A coarse golden-ratio step so a repair "new seed" is deterministic yet distinct.
_SEED_STEP = 0x9E3779B1
_SEED_MASK = 0x7FFFFFFF


# --------------------------------------------------------------------------- #
# Injected collaborator protocols (real services + agents satisfy these)
# --------------------------------------------------------------------------- #


class ShotRow(Protocol):
    """The slice of a ``shots`` row the pipeline reads."""

    id: str
    book_id: str
    beat_id: str | None
    scene_id: str | None
    source_span: dict[str, Any] | None
    duration_s: float | None
    shot_hash: str | None


class BeatRow(Protocol):
    """The slice of a ``beats`` row the pipeline reads."""

    id: str
    book_id: str
    scene_id: str
    beat_index: int
    summary: str
    entities: list[str]
    described_visuals: str | None
    mood: str | None
    source_span: dict[str, Any] | None


class PageRow(Protocol):
    """The slice of a ``pages`` row the pipeline reads (the word boxes for §9.4)."""

    word_boxes: list[dict[str, Any]] | None
    image_key: str | None
    text: str | None


class CachedRecord(Protocol):
    """The slice of a ``shot_cache`` row a cache hit exposes."""

    clip_key: str | None
    last_frame_key: str | None
    sync_segment: dict[str, Any] | None
    qa: dict[str, Any] | None
    video_seconds: float | None


class ShotOps(Protocol):
    """The ``ShotRepo`` seam: load + transition + patch a shot."""

    async def get(self, shot_id: str) -> ShotRow | None: ...

    async def set_status(self, shot_id: str, status: ShotStatus) -> None: ...

    async def mark_accepted(self, shot_id: str) -> None: ...

    async def update(self, shot_id: str, **fields: Any) -> ShotRow | None: ...


class BeatOps(Protocol):
    """The ``BeatRepo`` seam."""

    async def get(self, beat_id: str) -> BeatRow | None: ...


class PageOps(Protocol):
    """The ``PageRepo`` seam."""

    async def get_by_number(self, book_id: str, page_number: int) -> PageRow | None: ...


class DefectOps(Protocol):
    """The ``DefectRepo`` seam."""

    async def log(
        self,
        *,
        book_id: str,
        kind: str,
        shot_id: str | None = None,
        detail: dict[str, Any] | None = None,
        defect_id: str | None = None,
    ) -> Any: ...


class CanonReader(Protocol):
    """The ``CanonService.query`` seam (the §8.4 retrieval policy)."""

    async def query(self, book_id: str, beat_id: str) -> CanonSlice: ...


class CacheOps(Protocol):
    """The ``CacheService`` seam: content-hash compute + probe + populate (§8.7)."""

    def reference_set_hash(self, reference_image_ids: list[str]) -> str: ...

    def shot_hash(
        self,
        *,
        book_id: str,
        beat_id: str,
        canon_version_at_render: int,
        render_mode: str,
        seed: int,
        reference_set_hash: str,
    ) -> str: ...

    async def get(self, shot_hash: str) -> CachedRecord | None: ...

    async def put(
        self,
        *,
        shot_hash: str,
        book_id: str,
        clip_key: str | None = None,
        last_frame_key: str | None = None,
        sync_segment: dict[str, Any] | None = None,
        qa: dict[str, Any] | None = None,
        video_seconds: float | None = None,
    ) -> Any: ...


class BudgetOps(Protocol):
    """The ``BudgetService`` seam: the go-live gate + reserve/commit/release (§11.1)."""

    def can_render_live(self) -> bool: ...

    async def is_low(self) -> bool: ...

    async def reserve(
        self,
        video_seconds: float,
        *,
        session_id: str | None = None,
        scene_id: str | None = None,
        book_id: str | None = None,
        note: str | None = None,
    ) -> Reservation: ...

    async def commit(
        self,
        reservation: Reservation,
        actual_seconds: float | None = None,
        *,
        note: str | None = None,
    ) -> None: ...

    async def release(self, reservation: Reservation, *, note: str | None = None) -> None: ...


class EpisodicLog(Protocol):
    """The ``EpisodicService.log`` seam (§8.2 "what worked before")."""

    async def log(
        self,
        *,
        book_id: str,
        status: ShotStatus = ShotStatus.ACCEPTED,
        shot_id: str | None = None,
        beat_id: str | None = None,
        scene_id: str | None = None,
        source_span: dict[str, Any] | None = None,
        render_mode: str | None = None,
        prompt: str | None = None,
        negative_prompt: str | None = None,
        seed: int | None = None,
        reference_set_hash: str | None = None,
        reference_image_ids: list[str] | None = None,
        duration_s: float | None = None,
        output: dict[str, Any] | None = None,
        narration: dict[str, Any] | None = None,
        qa: dict[str, Any] | None = None,
        cost: dict[str, Any] | None = None,
        canon_version_at_render: int | None = None,
        shot_hash: str | None = None,
        last_frame_bytes: bytes | None = None,
    ) -> Any: ...


class ShotDesigner(Protocol):
    """The ``Cinematographer`` seam (§7.1 / §9.3 render-mode tree)."""

    async def design_shot(
        self,
        beat: AgentBeat,
        canon_slice: CanonSlice,
        director_notes: list[DirectorNote] | None = None,
        *,
        shot_id: str | None = None,
        target_duration_s: float = 5.0,
    ) -> AgentShotSpec: ...


class ClipGenerator(Protocol):
    """The ``Generator`` seam (real Wan + CosyVoice; raises ``LiveVideoDisabled``)."""

    async def render(
        self,
        spec: AgentShotSpec,
        *,
        narration_text: str,
        voice_id: str,
        reference_image_bytes: list[bytes] | None = None,
        prev_last_frame_bytes: bytes | None = None,
    ) -> GeneratorOutput: ...


class ClipCritic(Protocol):
    """The ``Critic`` seam (§9.5 thresholds + repair routing)."""

    async def score(
        self,
        *,
        shot_id: str,
        clip_frames: list[bytes],
        canon_slice: CanonSlice,
        character_crop: bytes | None = None,
        locked_ref_image: bytes | None = None,
        scene_style_centroid: list[float] | None = None,
        textual_evolution_supported: bool = False,
        retries_exhausted: bool = False,
    ) -> QARecord: ...


class Narrator(Protocol):
    """The ``TtsProvider.synthesize`` seam (narration + word timings, §9.4)."""

    async def synthesize(self, text: str, *, voice_id: str) -> TtsResult: ...


class ImageGen(Protocol):
    """The ``ImageProvider.generate`` seam (keyframe stills for degradation, §9.1)."""

    async def generate(self, prompt: str, *, n: int = 1) -> list[bytes]: ...


class ConflictResolving(Protocol):
    """The ``ConflictResolver`` seam (§7.2 Critic→Continuity→Showrunner→apply)."""

    async def resolve(
        self,
        *,
        book_id: str,
        shot_spec: AgentShotSpec | str,
        canon_slice: CanonSlice,
        source_span_text: str,
        current_beat_id: str,
        current_beat_index: int,
        director_present: bool,
        shot_id: str | None = None,
        target_duration_s: float = 5.0,
        textual_support: Any | None = None,
    ) -> ConflictResolution: ...


# --------------------------------------------------------------------------- #
# Result
# --------------------------------------------------------------------------- #


class RenderResult(BaseModel):
    """The outcome of a per-shot render call (§9.2)."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    shot_id: str
    status: ShotStatus
    #: ``full_video`` | ``cache_hit`` | a ``DegradeRung`` value.
    rung: str
    clip_key: str | None = None
    clip_url: str | None = None
    last_frame_key: str | None = None
    sync_segment: dict[str, Any] | None = None
    qa: dict[str, Any] | None = None
    video_seconds: float = 0.0
    cache_hit: bool = False
    conflict: ConflictObject | None = None
    attempts: int = 0


class UnknownShotError(LookupError):
    """Raised when ``render_shot`` is asked about a shot/beat that does not exist."""


def _rotate_seed(seed: int) -> int:
    """A deterministic, distinct next seed so a regen actually re-rolls (§9.5)."""
    return (int(seed) + _SEED_STEP) & _SEED_MASK


def _mean_vector(vectors: list[list[float]]) -> list[float]:
    """Component-wise mean of equal-length embedding vectors (the style centroid)."""
    if not vectors:
        return []
    length = len(vectors[0])
    sums = [0.0] * length
    for vec in vectors:
        for i in range(length):
            sums[i] += vec[i]
    count = float(len(vectors))
    return [s / count for s in sums]


@dataclass(slots=True)
class _RenderCtx:
    """The per-call context threaded through the orchestrator's helpers."""

    book_id: str
    shot_id: str
    session_id: str | None
    beat: BeatRow
    agent_beat: AgentBeat
    canon_slice: CanonSlice
    span: dict[str, Any]
    page: PageRow | None
    narration_text: str
    notes: list[DirectorNote]
    director_present: bool
    target_duration: float
    canon_version: int
    ref_hash: str
    shot_hash: str
    voice_id: str
    machine: ShotStateMachine
    spent_video_seconds: float = 0.0
    attempts: int = 0


@dataclass(slots=True)
class _ConflictOutcome:
    """Internal: a conflict either terminates the render or yields a regen spec."""

    terminal: RenderResult | None = None
    next_spec: AgentShotSpec | None = None
    next_notes: list[DirectorNote] | None = None


class RenderPipeline:
    """The §9.2/§9.7 per-shot Phase-B orchestrator (collaborators injected)."""

    def __init__(
        self,
        *,
        canon: CanonReader,
        episodic: EpisodicLog,
        cache: CacheOps,
        budget: BudgetOps,
        object_store: BlobStore,
        shots: ShotOps,
        beats: BeatOps,
        pages: PageOps,
        defects: DefectOps,
        designer: ShotDesigner,
        generator: ClipGenerator,
        critic: ClipCritic,
        narrator: Narrator,
        conflict_resolver: ConflictResolving | None = None,
        image_gen: ImageGen | None = None,
        embedder: Embedder | None = None,
        settings: Settings | None = None,
        default_voice: str = "Cherry",
        url_ttl: int = 3600,
    ) -> None:
        self._canon = canon
        self._episodic = episodic
        self._cache = cache
        self._budget = budget
        self._store = object_store
        self._shots = shots
        self._beats = beats
        self._pages = pages
        self._defects = defects
        self._designer = designer
        self._generator = generator
        self._critic = critic
        self._narrator = narrator
        self._conflict = conflict_resolver
        self._image_gen = image_gen
        # Embeds the scene Style node's locked reference into the §9.5 style
        # centroid the Critic measures drift against (None => style gate inert).
        self._embedder = embedder
        self._settings = settings or get_settings()
        self._default_voice = default_voice
        self._ttl = url_ttl

    @property
    def retry_cap(self) -> int:
        """The §9.5 repair retry cap (default 2)."""
        return self._settings.retry_cap

    async def render_shot(
        self,
        book_id: str,
        shot_id: str,
        *,
        session_id: str | None = None,
        director_notes: list[DirectorNote] | None = None,
        director_present: bool = False,
    ) -> RenderResult:
        """Render one shot end-to-end through the §9.7 state machine.

        Cache hit → cached clip at 0 video-seconds. Miss → design, then either
        the live Wan path (budget-reserved, Critic repair loop) or the real
        degradation ladder when the live gate is off / budget is low / retries
        are exhausted. Returns a :class:`RenderResult`. A thin timing wrapper
        records per-shot render latency (§12.5) without altering the logic.
        """
        started = time.perf_counter()
        result = await self._render_shot(
            book_id,
            shot_id,
            session_id=session_id,
            director_notes=director_notes,
            director_present=director_present,
        )
        metrics.observe_render_latency(result.rung, time.perf_counter() - started)
        return result

    async def _render_shot(
        self,
        book_id: str,
        shot_id: str,
        *,
        session_id: str | None = None,
        director_notes: list[DirectorNote] | None = None,
        director_present: bool = False,
    ) -> RenderResult:
        shot = await self._shots.get(shot_id)
        if shot is None or shot.book_id != book_id:
            raise UnknownShotError(f"unknown shot for book {book_id}: {shot_id}")

        async def _persist(_state: RenderState, status: ShotStatus) -> None:
            await self._shots.set_status(shot_id, status)

        machine = ShotStateMachine(shot_id, state=RenderState.PROMOTED, on_transition=_persist)

        # Fast re-read path: a known shot_hash that is already cached (§8.7).
        if shot.shot_hash:
            cached = await self._cache.get(shot.shot_hash)
            if cached is not None and cached.clip_key:
                await machine.transition(RenderState.CACHE_CHECK)
                await machine.transition(RenderState.ACCEPTED)
                return await self._cache_hit_result(shot_id, cached)

        beat_id = shot.beat_id
        if beat_id is None:
            raise UnknownShotError(f"shot {shot_id} has no beat to render")
        beat = await self._beats.get(beat_id)
        if beat is None:
            raise UnknownShotError(f"unknown beat for shot {shot_id}: {beat_id}")

        canon_slice = await self._canon.query(book_id, beat_id)
        agent_beat = self._to_agent_beat(beat)
        span = self._span(shot, beat)
        page = await self._load_page(book_id, span)
        narration_text = self._narration_text(span, page, beat)
        notes = list(director_notes or [])
        target_duration = float(shot.duration_s or 5.0)

        await machine.transition(RenderState.CACHE_CHECK)
        spec = await self._designer.design_shot(
            agent_beat, canon_slice, notes, shot_id=shot_id, target_duration_s=target_duration
        )
        canon_version = self._canon_version(canon_slice)
        ref_hash = self._cache.reference_set_hash(spec.reference_image_ids)
        shot_hash = self._cache.shot_hash(
            book_id=book_id,
            beat_id=beat_id,
            canon_version_at_render=canon_version,
            render_mode=spec.render_mode.value,
            seed=spec.seed,
            reference_set_hash=ref_hash,
        )

        ctx = _RenderCtx(
            book_id=book_id,
            shot_id=shot_id,
            session_id=session_id,
            beat=beat,
            agent_beat=agent_beat,
            canon_slice=canon_slice,
            span=span,
            page=page,
            narration_text=narration_text,
            notes=notes,
            director_present=director_present,
            target_duration=target_duration,
            canon_version=canon_version,
            ref_hash=ref_hash,
            shot_hash=shot_hash,
            voice_id=self._voice_id(canon_slice),
            machine=machine,
        )

        # Post-design cache probe (the full content hash is now known).
        cached = await self._cache.get(shot_hash)
        if cached is not None and cached.clip_key:
            await self._shots.update(shot_id, shot_hash=shot_hash, reference_set_hash=ref_hash)
            await machine.transition(RenderState.ACCEPTED)
            return await self._cache_hit_result(shot_id, cached)
        metrics.inc_cache(hit=False)  # a real miss: design done, must render/degrade

        # Budget-aware degradation: live gate off or remaining below the floor.
        if not self._budget.can_render_live() or await self._budget.is_low():
            await machine.transition(RenderState.RENDERING)
            reason = "live_video_disabled" if not self._budget.can_render_live() else "budget_low"
            return await self._degrade(ctx, spec, reason=reason, qa=None, spent_video_seconds=0.0)

        return await self._render_live_loop(ctx, spec)

    # -- the live Wan path + the §9.5 self-correcting repair loop ------------ #

    async def _render_live_loop(self, ctx: _RenderCtx, spec: AgentShotSpec) -> RenderResult:
        """Reserve → render → QA → (accept | repair | conflict | degrade), retry ≤ cap."""
        ref_bytes = await self._reference_bytes(ctx.canon_slice, spec)
        prev_frame = await self._prev_last_frame(ctx.canon_slice)
        locked_ref = await self._first_locked_ref_bytes(ctx.canon_slice)
        # §9.5 style gate: a real scene style centroid (the Style node's locked
        # reference embedding) so the Critic measures style_drift, not a no-op None.
        style_centroid = await self._scene_style_centroid(ctx.canon_slice)
        cur_spec = spec
        cur_notes = list(ctx.notes)

        for attempt in range(self.retry_cap + 1):
            ctx.attempts = attempt + 1
            retries_exhausted = attempt == self.retry_cap
            await ctx.machine.transition(RenderState.RENDERING)

            try:
                reservation = await self._budget.reserve(
                    ctx.target_duration,
                    session_id=ctx.session_id,
                    scene_id=ctx.beat.scene_id,
                    book_id=ctx.book_id,
                    note=f"render {ctx.shot_id} attempt {attempt}",
                )
            except BudgetExceeded:
                return await self._degrade(ctx, cur_spec, reason="budget_exceeded", qa=None)

            try:
                output = await self._generator.render(
                    cur_spec,
                    narration_text=ctx.narration_text,
                    voice_id=ctx.voice_id,
                    reference_image_bytes=ref_bytes,
                    prev_last_frame_bytes=prev_frame,
                )
            except (LiveVideoDisabled, ProviderError) as exc:
                await self._budget.release(reservation)
                return await self._degrade(ctx, cur_spec, reason=type(exc).__name__, qa=None)

            actual = float(output.duration_s or ctx.target_duration)
            await ctx.machine.transition(RenderState.QA)
            frames = await self._frames(output)
            try:
                qa = await self._critic.score(
                    shot_id=ctx.shot_id,
                    clip_frames=frames,
                    canon_slice=ctx.canon_slice,
                    character_crop=frames[0] if frames else None,
                    locked_ref_image=locked_ref,
                    scene_style_centroid=style_centroid,
                    retries_exhausted=retries_exhausted,
                )
            except (LiveVideoDisabled, ProviderError) as exc:
                # The clip rendered (its seconds were really spent), but QA is
                # unavailable. Commit the spend and ship a degraded, playable
                # result rather than letting the worker retry into the DLQ — the
                # film never hard-stops (§4.11/§12.4). A failed QA is a REPAIR, so
                # step QA -> REPAIR before the ladder (the legal §9.7 edge).
                await self._budget.commit(reservation, actual)
                ctx.spent_video_seconds += actual
                await ctx.machine.transition(RenderState.REPAIR)
                return await self._degrade(
                    ctx, cur_spec, reason=f"critic_{type(exc).__name__}", qa=None
                )

            if qa.verdict is Verdict.PASS:
                await self._budget.commit(reservation, actual)
                ctx.spent_video_seconds += actual
                await ctx.machine.transition(RenderState.ACCEPTED)
                return await self._accept(ctx, cur_spec, output, qa)

            # A failed attempt still spent its seconds — charge them.
            await self._budget.commit(reservation, actual)
            ctx.spent_video_seconds += actual
            await ctx.machine.transition(RenderState.REPAIR)

            if qa.repair_action is RepairAction.DEGRADE or retries_exhausted:
                return await self._degrade(ctx, cur_spec, reason="retries_exhausted", qa=qa)

            if qa.repair_action in (RepairAction.RAISE_CONFLICT, RepairAction.EVOLVE_CANON):
                result = await self._handle_conflict(ctx, cur_spec, qa, output)
                if result.terminal is not None:
                    return result.terminal
                cur_spec = result.next_spec or cur_spec
                cur_notes = result.next_notes or cur_notes
                continue

            cur_spec, cur_notes = await self._apply_repair(
                ctx, qa.repair_action, cur_spec, cur_notes
            )

        # Unreachable: the final attempt always degrades via ``retries_exhausted``.
        return await self._degrade(ctx, cur_spec, reason="loop_exhausted", qa=None)

    async def _apply_repair(
        self,
        ctx: _RenderCtx,
        action: RepairAction,
        spec: AgentShotSpec,
        notes: list[DirectorNote],
    ) -> tuple[AgentShotSpec, list[DirectorNote]]:
        """Route a non-conflict repair to an adjusted spec for the next attempt (§9.5)."""
        if action is RepairAction.REGEN_NEW_SEED:
            logger.info("repair.new_seed", shot_id=ctx.shot_id)
            return spec.model_copy(update={"seed": _rotate_seed(spec.seed)}), notes
        if action is RepairAction.REGEN_TIGHTEN_REFS:
            directive = (
                "Identity drift: re-lock the character to the reference set; tighten the face."
            )
        else:  # REPROMPT_STYLE
            directive = (
                "Style drift: reinforce the scene style tokens (palette, lens, art direction)."
            )
        logger.info("repair.redesign", shot_id=ctx.shot_id, action=action.value)
        next_notes = [*notes, DirectorNote(shot_id=ctx.shot_id, note=directive)]
        next_spec = await self._redesign(ctx, next_notes, spec.seed)
        return next_spec, next_notes

    async def _handle_conflict(
        self,
        ctx: _RenderCtx,
        spec: AgentShotSpec,
        qa: QARecord,
        output: GeneratorOutput,
    ) -> _ConflictOutcome:
        """Route a timeline failure through §7.2 and apply honor/evolve/surface."""
        await ctx.machine.transition(RenderState.CONFLICT)
        if self._conflict is None:
            logger.warning("conflict.no_resolver", shot_id=ctx.shot_id)
            terminal = await self._degrade(ctx, spec, reason="no_conflict_resolver", qa=qa)
            return _ConflictOutcome(terminal=terminal)

        resolution = await self._conflict.resolve(
            book_id=ctx.book_id,
            shot_spec=spec,
            canon_slice=ctx.canon_slice,
            source_span_text=ctx.narration_text,
            current_beat_id=ctx.beat.id,
            current_beat_index=ctx.beat.beat_index,
            director_present=ctx.director_present,
            shot_id=ctx.shot_id,
            target_duration_s=ctx.target_duration,
        )

        if resolution.action == "surface":
            return _ConflictOutcome(terminal=self._conflict_result(ctx, qa, resolution.conflict))
        if resolution.action == "accept":
            # §7.2 Checked → Approved: Continuity cleared the alarm; accept as-is.
            await ctx.machine.transition(RenderState.ACCEPTED)
            return _ConflictOutcome(terminal=await self._accept(ctx, spec, output, qa))

        # honor_canon / evolve_canon → regenerate with the directive folded in.
        next_notes = [
            *ctx.notes,
            DirectorNote(shot_id=ctx.shot_id, note=resolution.regen_directive),
        ]
        next_spec = await self._redesign(ctx, next_notes, spec.seed)
        return _ConflictOutcome(next_spec=next_spec, next_notes=next_notes)

    async def _redesign(
        self, ctx: _RenderCtx, notes: list[DirectorNote], prev_seed: int
    ) -> AgentShotSpec:
        """Re-call the Cinematographer for a repair, re-rolling the seed (§9.5)."""
        spec = await self._designer.design_shot(
            ctx.agent_beat,
            ctx.canon_slice,
            notes,
            shot_id=ctx.shot_id,
            target_duration_s=ctx.target_duration,
        )
        return spec.model_copy(update={"seed": _rotate_seed(prev_seed)})

    # -- accept (§9.6: write outputs, log episodic, cache, anchor canon) ----- #

    async def _accept(
        self,
        ctx: _RenderCtx,
        spec: AgentShotSpec,
        output: GeneratorOutput,
        qa: QARecord,
    ) -> RenderResult:
        """Persist an accepted shot: OSS + episodic + cache + continuation anchor."""
        duration = float(output.duration_s or ctx.target_duration)

        clip_key: str | None = None
        clip_url: str | None = output.clip_url
        if output.clip_bytes:
            clip_key = keys.clip(ctx.book_id, ctx.shot_id)
            await self._put_bytes(clip_key, output.clip_bytes, "video/mp4")
            clip_url = await self._presign(clip_key)

        last_frame_key: str | None = None
        if output.last_frame_bytes:
            last_frame_key = keys.lastframe(ctx.book_id, ctx.shot_id)
            await self._put_bytes(last_frame_key, output.last_frame_bytes, "image/png")

        audio_key: str | None = None
        if output.audio_bytes:
            audio_key = keys.audio(ctx.book_id, ctx.shot_id)
            await self._put_bytes(audio_key, output.audio_bytes, "audio/wav")

        segment = build_sync_segment(
            shot_id=ctx.shot_id,
            word_timestamps=output.word_timestamps,
            source_span=ctx.span,
            page_word_boxes=self._page_boxes(ctx.page),
            duration_s=duration,
        )
        qa_dict = qa.model_dump(mode="json")
        narration = {
            "text": ctx.narration_text,
            "audio_key": audio_key,
            "word_timestamps": [w.model_dump(mode="json") for w in output.word_timestamps],
            "sync_segment": segment.model_dump(mode="json"),
        }
        output_payload = {
            "clip_key": clip_key,
            "clip_url": clip_url,
            "last_frame_key": last_frame_key,
        }

        await self._episodic.log(
            book_id=ctx.book_id,
            status=ShotStatus.ACCEPTED,
            shot_id=ctx.shot_id,
            beat_id=ctx.beat.id,
            scene_id=ctx.beat.scene_id,
            source_span=ctx.span or None,
            render_mode=spec.render_mode.value,
            prompt=spec.prompt,
            negative_prompt=spec.negative_prompt,
            seed=spec.seed,
            reference_set_hash=ctx.ref_hash,
            reference_image_ids=spec.reference_image_ids,
            duration_s=duration,
            output=output_payload,
            narration=narration,
            qa=qa_dict,
            cost={"video_seconds": duration, "tokens": 0},
            canon_version_at_render=ctx.canon_version,
            shot_hash=ctx.shot_hash,
            last_frame_bytes=output.last_frame_bytes,
        )
        await self._shots.mark_accepted(ctx.shot_id)
        await self._cache.put(
            shot_hash=ctx.shot_hash,
            book_id=ctx.book_id,
            clip_key=clip_key,
            last_frame_key=last_frame_key,
            sync_segment=segment.model_dump(mode="json"),
            qa=qa_dict,
            video_seconds=duration,
        )
        # The last accepted frame is the continuation anchor (§9.6): canon.query's
        # previous_endpoint reads this shot's output.last_frame_key.
        logger.info(
            "canon.continuation_anchor",
            shot_id=ctx.shot_id,
            last_frame_key=last_frame_key,
            beat=ctx.beat.id,
        )
        metrics.inc_render_mode(spec.render_mode.value)
        metrics.inc_video_seconds(ctx.spent_video_seconds)
        metrics.inc_shot_accepted()
        metrics.inc_render_retries(max(ctx.attempts - 1, 0))
        metrics.observe_qa(ccs=qa.ccs, style_drift=qa.style_drift, motion=qa.motion_artifact)
        return RenderResult(
            shot_id=ctx.shot_id,
            status=ShotStatus.ACCEPTED,
            rung="full_video",
            clip_key=clip_key,
            clip_url=clip_url,
            last_frame_key=last_frame_key,
            sync_segment=segment.model_dump(mode="json"),
            qa=qa_dict,
            video_seconds=duration,
            attempts=ctx.attempts,
        )

    # -- degrade (§4.4/§12.4: the REAL ffmpeg Ken-Burns ladder) -------------- #

    async def _degrade(
        self,
        ctx: _RenderCtx,
        spec: AgentShotSpec,
        *,
        reason: str,
        qa: QARecord | None,
        spent_video_seconds: float | None = None,
    ) -> RenderResult:
        """Step down the ladder: a real Ken-Burns (or audio card) mp4 + a defect."""
        spent = ctx.spent_video_seconds if spent_video_seconds is None else spent_video_seconds
        # Narration may itself fail under a provider outage — never let that turn a
        # degrade into a crash/DLQ. A TTS failure yields a *silent* (no-audio) but
        # still playable Ken-Burns/text card (§4.11/§12.4).
        tts: TtsResult | None
        try:
            tts = await self._narrator.synthesize(ctx.narration_text, voice_id=ctx.voice_id)
        except (LiveVideoDisabled, ProviderError) as exc:
            logger.warning("degrade.tts_failed", shot_id=ctx.shot_id, error=str(exc))
            tts = None
        audio_bytes = tts.audio_bytes or None if tts is not None else None
        audio_dur = float(tts.duration_s or 0.0) if tts is not None else 0.0
        word_timestamps = list(tts.word_timestamps) if tts is not None else []
        alignment = tts.alignment if tts is not None else None
        clip_dur = (
            max(ctx.target_duration, math.ceil(audio_dur)) if audio_dur > 0 else ctx.target_duration
        )
        clip_dur = max(clip_dur, 1.0)

        still, rung = await self._select_keyframe(ctx, spec)
        last_frame_bytes: bytes | None = None
        if still is not None:
            clip_bytes = await anyio.to_thread.run_sync(
                lambda: ken_burns_over_image(still, clip_dur, audio_bytes=audio_bytes)
            )
            last_frame_bytes = still
        else:
            rung = DegradeRung.AUDIO_TEXT_ONLY
            clip_bytes = await anyio.to_thread.run_sync(
                lambda: audio_text_card(clip_dur, audio_bytes=audio_bytes)
            )

        clip_key = keys.clip(ctx.book_id, ctx.shot_id)
        await self._put_bytes(clip_key, clip_bytes, "video/mp4")
        clip_url = await self._presign(clip_key)
        audio_key: str | None = None
        if audio_bytes:
            audio_key = keys.audio(ctx.book_id, ctx.shot_id)
            await self._put_bytes(audio_key, audio_bytes, "audio/wav")
        last_frame_key: str | None = None
        if last_frame_bytes is not None:
            last_frame_key = keys.lastframe(ctx.book_id, ctx.shot_id)
            await self._put_bytes(last_frame_key, last_frame_bytes, "image/png")

        segment = build_sync_segment(
            shot_id=ctx.shot_id,
            word_timestamps=word_timestamps,
            source_span=ctx.span,
            page_word_boxes=self._page_boxes(ctx.page),
            duration_s=clip_dur,
        )
        qa_dict = qa.model_dump(mode="json") if qa is not None else None
        narration = {
            "text": ctx.narration_text,
            "audio_key": audio_key,
            "word_timestamps": [w.model_dump(mode="json") for w in word_timestamps],
            "sync_segment": segment.model_dump(mode="json"),
            "alignment": alignment,
        }
        await self._shots.update(
            ctx.shot_id,
            render_mode=spec.render_mode.value,
            seed=spec.seed,
            reference_set_hash=ctx.ref_hash,
            reference_image_ids=spec.reference_image_ids,
            duration_s=clip_dur,
            shot_hash=ctx.shot_hash,
            canon_version_at_render=ctx.canon_version,
            output={"clip_key": clip_key, "clip_url": clip_url, "last_frame_key": last_frame_key},
            narration=narration,
            qa=qa_dict,
        )
        await ctx.machine.transition(RenderState.DEGRADED)
        await self._defects.log(
            book_id=ctx.book_id,
            kind="degraded",
            shot_id=ctx.shot_id,
            detail={"rung": rung.value, "reason": reason, "qa": qa_dict},
        )
        logger.info(
            "degrade.shipped",
            shot_id=ctx.shot_id,
            rung=rung.value,
            reason=reason,
            duration_s=round(clip_dur, 3),
            spent_video_seconds=round(spent, 3),
        )
        metrics.inc_shot_degraded()
        metrics.inc_render_mode(spec.render_mode.value)
        metrics.inc_video_seconds(spent)
        metrics.inc_render_retries(max(ctx.attempts - 1, 0))
        return RenderResult(
            shot_id=ctx.shot_id,
            status=ShotStatus.DEGRADED,
            rung=rung.value,
            clip_key=clip_key,
            clip_url=clip_url,
            last_frame_key=last_frame_key,
            sync_segment=segment.model_dump(mode="json"),
            qa=qa_dict,
            video_seconds=spent,
            attempts=ctx.attempts,
        )

    async def _cache_hit_result(self, shot_id: str, cached: CachedRecord) -> RenderResult:
        """A cache hit serves the cached clip at zero video-seconds (§8.7)."""
        metrics.inc_cache(hit=True)
        logger.info("cache.hit", shot_id=shot_id, clip_key=cached.clip_key)
        return RenderResult(
            shot_id=shot_id,
            status=ShotStatus.ACCEPTED,
            rung="cache_hit",
            clip_key=cached.clip_key,
            clip_url=await self._presign(cached.clip_key) if cached.clip_key else None,
            last_frame_key=cached.last_frame_key,
            sync_segment=cached.sync_segment,
            qa=cached.qa,
            video_seconds=0.0,
            cache_hit=True,
        )

    def _conflict_result(
        self, ctx: _RenderCtx, qa: QARecord, conflict: ConflictObject | None
    ) -> RenderResult:
        """Surface an unresolved conflict for the director to choose (§7.2)."""
        logger.info(
            "conflict.surfaced",
            shot_id=ctx.shot_id,
            conflict_id=conflict.conflict_id if conflict else None,
        )
        metrics.inc_conflict()
        metrics.inc_video_seconds(ctx.spent_video_seconds)
        return RenderResult(
            shot_id=ctx.shot_id,
            status=ShotStatus.CONFLICT,
            rung="conflict",
            qa=qa.model_dump(mode="json"),
            conflict=conflict,
            video_seconds=ctx.spent_video_seconds,
            attempts=ctx.attempts,
        )

    # -- keyframe selection for the ladder ----------------------------------- #

    async def _select_keyframe(
        self, ctx: _RenderCtx, spec: AgentShotSpec
    ) -> tuple[bytes | None, DegradeRung]:
        """Pick the best available still for Ken-Burns, walking down the rungs."""
        kf_key = keys.keyframe(ctx.book_id, ctx.beat.id)
        if await self._exists(kf_key):
            return await self._get_bytes(kf_key), DegradeRung.KEN_BURNS_KEYFRAME

        locked = await self._first_locked_ref_bytes(ctx.canon_slice)
        if locked is not None:
            return locked, DegradeRung.KEN_BURNS_KEYFRAME

        prev = await self._prev_last_frame(ctx.canon_slice)
        if prev is not None:
            return prev, DegradeRung.KEN_BURNS_KEYFRAME

        if self._image_gen is not None:
            prompt = spec.prompt or ctx.beat.described_visuals or ctx.beat.summary
            try:
                images = await self._image_gen.generate(prompt, n=1)
            except ProviderError:
                images = []
            if images:
                return images[0], DegradeRung.KEN_BURNS_KEYFRAME

        if ctx.page is not None and ctx.page.image_key and await self._exists(ctx.page.image_key):
            return await self._get_bytes(ctx.page.image_key), DegradeRung.KEN_BURNS_ILLUSTRATION

        return None, DegradeRung.AUDIO_TEXT_ONLY

    # -- small derivations --------------------------------------------------- #

    def _to_agent_beat(self, beat: BeatRow) -> AgentBeat:
        span = beat.source_span or {}
        return AgentBeat(
            beat_id=beat.id,
            scene_id=beat.scene_id,
            beat_index=beat.beat_index,
            summary=beat.summary,
            entities=list(beat.entities or []),
            described_visuals=beat.described_visuals,
            mood=beat.mood,
            source_span=SourceSpan.model_validate(span) if span else SourceSpan(),
        )

    @staticmethod
    def _span(shot: ShotRow, beat: BeatRow) -> dict[str, Any]:
        return dict(shot.source_span or beat.source_span or {})

    async def _load_page(self, book_id: str, span: Mapping[str, Any]) -> PageRow | None:
        page_no = span.get("page")
        if not page_no:
            return None
        return await self._pages.get_by_number(book_id, int(page_no))

    @staticmethod
    def _page_boxes(page: PageRow | None) -> list[dict[str, Any]] | None:
        return page.word_boxes if page is not None else None

    def _narration_text(self, span: Mapping[str, Any], page: PageRow | None, beat: BeatRow) -> str:
        boxes = self._page_boxes(page)
        rng = span.get("word_range")
        if (
            boxes
            and isinstance(rng, (list, tuple))
            and len(rng) == 2
            and not (int(rng[0]) == 0 and int(rng[1]) == 0)
        ):
            start, end = int(rng[0]), int(rng[1])
            words = [
                str(box.get("text", ""))
                for box in boxes
                if start <= int(box.get("word_index", -1)) <= end
            ]
            text = " ".join(word for word in words if word).strip()
            if text:
                return text
        summary = (beat.summary or "").strip()
        if summary:
            return summary
        return (page.text or "").strip() if page is not None and page.text else ""

    @staticmethod
    def _canon_version(canon_slice: CanonSlice) -> int:
        versions = [c.version for c in canon_slice.characters]
        versions += [p.version for p in canon_slice.props]
        if canon_slice.location is not None:
            versions.append(canon_slice.location.version)
        if canon_slice.style is not None:
            versions.append(canon_slice.style.version)
        return max(versions, default=1)

    def _voice_id(self, canon_slice: CanonSlice) -> str:
        for character in canon_slice.characters:
            if character.voice:
                vid = character.voice.get("cosyvoice_voice_id") or character.voice.get("voice_id")
                if isinstance(vid, str) and vid:
                    return vid
        return self._default_voice

    @staticmethod
    def _entities(canon_slice: CanonSlice) -> list[Any]:
        loc = [canon_slice.location] if canon_slice.location is not None else []
        return [*canon_slice.characters, *loc, *canon_slice.props]

    async def _reference_bytes(self, canon_slice: CanonSlice, spec: AgentShotSpec) -> list[bytes]:
        """Resolve the spec's locked-ref ids (``key@vN``) to real image bytes."""
        keys_by_id: dict[str, list[str]] = {}
        for entity in self._entities(canon_slice):
            ent_id = f"{entity.entity_key}@v{entity.version}"
            locked = [ref.key for ref in entity.reference_images if ref.locked and ref.key]
            if locked:
                keys_by_id[ent_id] = locked
        out: list[bytes] = []
        for ref_id in spec.reference_image_ids:
            for key in keys_by_id.get(ref_id, []):
                if await self._exists(key):
                    out.append(await self._get_bytes(key))
        return out

    async def _prev_last_frame(self, canon_slice: CanonSlice) -> bytes | None:
        endpoint = canon_slice.previous_endpoint
        if endpoint is None or not endpoint.last_frame_key:
            return None
        if await self._exists(endpoint.last_frame_key):
            return await self._get_bytes(endpoint.last_frame_key)
        return None

    async def _first_locked_ref_bytes(self, canon_slice: CanonSlice) -> bytes | None:
        for entity in self._entities(canon_slice):
            for ref in entity.reference_images:
                if ref.locked and ref.key and await self._exists(ref.key):
                    return await self._get_bytes(ref.key)
        return None

    async def _scene_style_centroid(self, canon_slice: CanonSlice) -> list[float] | None:
        """The §9.5 scene style centroid the Critic measures style_drift against.

        It is the mean embedding of the scene Style node's locked reference
        image(s) (``canon.query`` already attaches the Style node to the slice).
        Returns ``None`` only when there is no embedder, no style node, or no
        present locked style reference — otherwise the style gate is *live*.
        """
        if self._embedder is None or canon_slice.style is None:
            return None
        images: list[bytes] = []
        for ref in canon_slice.style.reference_images:
            if ref.locked and ref.key and await self._exists(ref.key):
                images.append(await self._get_bytes(ref.key))
        if not images:
            return None
        vectors = await self._embedder.embed_images(images)
        return _mean_vector(vectors) if vectors else None

    async def _frames(self, output: GeneratorOutput) -> list[bytes]:
        if not output.clip_bytes:
            return []
        try:
            return await anyio.to_thread.run_sync(extract_frames, output.clip_bytes, 4)
        except FfmpegError:
            return []

    # -- object-store async wrappers (boto3 is sync) ------------------------- #

    async def _get_bytes(self, key: str) -> bytes:
        return await anyio.to_thread.run_sync(self._store.get_bytes, key)

    async def _exists(self, key: str) -> bool:
        return await anyio.to_thread.run_sync(self._store.exists, key)

    async def _put_bytes(self, key: str, data: bytes, content_type: str) -> None:
        await anyio.to_thread.run_sync(self._store.put_bytes, key, data, content_type)

    async def _presign(self, key: str) -> str:
        return await anyio.to_thread.run_sync(
            lambda: self._store.presigned_get_url(key, ttl=self._ttl)
        )


def build_render_pipeline(
    session: Any,
    *,
    providers: Any,
    object_store: Any,
    settings: Settings | None = None,
    default_voice: str = "Cherry",
    url_ttl: int = 3600,
) -> RenderPipeline:
    """Wire a production :class:`RenderPipeline` from the real services/agents.

    The collaborators built in earlier phases — ``CanonService`` /
    ``EpisodicService`` / ``CacheService`` / ``BudgetService``, the repositories,
    the Cinematographer / Generator / Critic, and the §7.2 ``ConflictResolver``
    (Continuity + Showrunner) — are constructed against one async ``session`` +
    the shared ``Providers`` + the ``ObjectStore``. Heavy imports are local so
    importing this module stays cheap.
    """
    settings = settings or get_settings()

    from app.agents.cinematographer import Cinematographer
    from app.agents.continuity import Continuity
    from app.agents.critic import Critic
    from app.agents.generator import Generator
    from app.agents.showrunner import Showrunner
    from app.db.repositories.beat import BeatRepo
    from app.db.repositories.book import PageRepo
    from app.db.repositories.budget import BudgetRepo
    from app.db.repositories.defect import DefectRepo
    from app.db.repositories.shot import ShotCacheRepo, ShotRepo
    from app.memory.budget_service import BudgetLimits, BudgetService
    from app.memory.cache_service import CacheService
    from app.memory.canon_service import CanonService
    from app.memory.episodic_service import EpisodicService
    from app.render.conflict import ConflictResolver

    shots = ShotRepo(session)
    embedder = providers.embeddings
    canon = CanonService(session, embedder=embedder, blob_store=object_store, url_ttl=url_ttl)
    episodic = EpisodicService(
        shots=shots, embedder=embedder, blob_store=object_store, url_ttl=url_ttl
    )
    cache = CacheService(cache=ShotCacheRepo(session), blob_store=object_store, url_ttl=url_ttl)
    budget = BudgetService(repo=BudgetRepo(session), limits=BudgetLimits.from_settings(settings))
    resolver = ConflictResolver(
        continuity=Continuity(providers, settings=settings),
        showrunner=Showrunner(providers, settings=settings),
        canon=canon,
    )
    return RenderPipeline(
        canon=canon,
        episodic=episodic,
        cache=cache,
        budget=budget,
        object_store=object_store,
        shots=shots,
        beats=BeatRepo(session),
        pages=PageRepo(session),
        defects=DefectRepo(session),
        designer=Cinematographer(providers, settings=settings),
        generator=Generator(providers),
        critic=Critic(providers, settings=settings),
        narrator=providers.tts,
        conflict_resolver=resolver,
        image_gen=providers.image,
        embedder=embedder,
        settings=settings,
        default_voice=default_voice,
        url_ttl=url_ttl,
    )


__all__ = [
    "BudgetOps",
    "CacheOps",
    "CanonReader",
    "ClipCritic",
    "ClipGenerator",
    "ConflictResolving",
    "DefectOps",
    "EpisodicLog",
    "ImageGen",
    "Narrator",
    "RenderPipeline",
    "RenderResult",
    "ShotDesigner",
    "UnknownShotError",
    "build_render_pipeline",
]
