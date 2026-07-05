"""LiveEventShotRenderer — the adapter that lets the existing Generator agent
(which already owns the still-bytes→WanSpec translation via build_wan_spec,
and the live VideoBackend/VideoRouter call) drive EventDirector's concurrent
multi-shot rendering, WITHOUT dropping the per-shot Critic gate the
shot-granularity live path already runs (RenderPipeline._render_shot). Two
accuracy layers stay intact: this class enforces the per-shot layer;
EventDirector's own _score_continuity enforces the seam layer on top.

Argument sourcing mirrors ``RenderPipeline._render_shot`` / ``_render_live_loop``
(``backend/app/render/pipeline.py``, read in full before writing this module):
narration text falls back to the beat/segment summary — the same fallback
``RenderPipeline._narration_text`` uses when there is no page-word-box match,
which is this adapter's *only* available source since it has no ``PageOps``;
the voice id is the first locked character's voice pulled off a real
``CanonSlice``, exactly like ``RenderPipeline._voice_id``; the Critic's frames
are the real sampled clip frames from :func:`app.render.degrade.extract_frames`
(never the raw clip bytes), exactly like ``RenderPipeline._frames``; when an
``object_store`` (and, for the style check, an ``embedder``) is wired, the
Critic's identity/style-drift checks run against real signal too —
``locked_ref_image`` is the slice's first present locked reference image
(mirroring ``RenderPipeline._first_locked_ref_bytes``) and
``scene_style_centroid`` is the mean embedding of the scene Style node's
locked reference(s) (mirroring ``RenderPipeline._scene_style_centroid``); a
``ProviderError`` / ``LiveVideoDisabled`` raised by either ``Generator.render``
or ``Critic.score`` is caught exactly like ``RenderPipeline._render_live_loop``
catches it around the same two calls, so a provider outage degrades just this
shot instead of propagating out of ``EventDirector.render_event``'s bare
``asyncio.gather`` (which has no ``try/except`` of its own and would otherwise
crash the whole event); the degrade fallback is the same real Ken-Burns /
audio-text-card rung :class:`~app.render.event_director.KenBurnsEventRenderer`
already produces for the off-gate path.

Known, disclosed simplifications versus the shot-granularity pipeline (see the
Task 6 report for the full rationale — these are deliberate, not oversights):

* ``reference_image_ids`` / ``end_frame_ref`` are left at ``ShotSpec``'s own
  defaults (empty list / ``None``). An ``EventShot`` carries no equivalent of
  the Cinematographer-selected canon reference ids: ``plan_event_script`` /
  ``plan_segment_script`` never call a Cinematographer, so there is no single
  obviously-correct source for either field from an ``EventShot`` alone.
* ``prev_last_frame_bytes`` is always ``None``. ``EventDirector.render_event``
  fans every shot in an event out *concurrently* (``asyncio.gather``), so a
  sibling shot's accepted last frame is structurally unavailable when this
  shot starts rendering — true frame-to-frame continuation would need the
  fan-out itself to become dependency-ordered, which is out of this adapter's
  scope.
* ``locked_ref_image`` / ``scene_style_centroid`` fall back to ``None`` (the
  Critic's own documented "gate inert" values, ``Critic._ccs`` /
  ``Critic._style_drift``) only when ``object_store`` / ``embedder`` are left
  unwired, there is no locked reference present yet, or (for style) there is
  no Style node on the slice — the same residual cases ``RenderPipeline``
  itself falls back for. A caller that wires the same ``BlobStore`` given to
  ``EventDirector(store=...)`` (and the shared ``providers.embeddings``) gets
  full parity with the shot-granularity gate.
* the three retryable ``repair_action``s (``REGEN_TIGHTEN_REFS`` /
  ``REPROMPT_STYLE`` / ``REGEN_NEW_SEED``) are treated uniformly as "try
  again with the same spec" rather than replicating each distinct tightening
  strategy (seed rotation vs. a full Cinematographer redesign call) —
  ``RenderPipeline``'s job, explicitly out of scope for this event-level
  adapter's first version.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import anyio

from app.agents.contracts import RepairAction, ShotSpec, Verdict
from app.agents.generator import GeneratorOutput
from app.core.logging import get_logger
from app.memory.budget_service import BudgetExceeded
from app.memory.interfaces import BlobStore, CanonEntitySlice, CanonSlice, Embedder
from app.providers.errors import LiveVideoDisabled, ProviderError
from app.render.degrade import (
    DEFAULT_FPS,
    FILM_SIZE,
    FfmpegError,
    audio_text_card,
    extract_frames,
    ken_burns_over_image,
    zoom_for_camera,
)
from app.render.event_director import EventShot, RenderedShot
from app.render.pipeline import BudgetOps, CanonReader, ClipCritic, ClipGenerator, DefectOps

logger = get_logger("app.render.live_event_renderer")

#: repair_action values worth one more render attempt with the same shot spec
#: (this adapter does not replicate each strategy's distinct tightening — that
#: stays RenderPipeline's job — it just tries again, up to max_retries).
_RETRYABLE = frozenset(
    {RepairAction.REGEN_TIGHTEN_REFS, RepairAction.REPROMPT_STYLE, RepairAction.REGEN_NEW_SEED}
)
#: repair_action values this per-shot adapter cannot resolve alone (need
#: Continuity/Showrunner arbitration or canon editing) — degrade immediately,
#: do not burn retries, but log it so it surfaces in the campaign's defect log.
_NEEDS_ARBITRATION = frozenset({RepairAction.RAISE_CONFLICT, RepairAction.EVOLVE_CANON})


def _mean_vector(vectors: list[list[float]]) -> list[float]:
    """Component-wise mean of equal-length embedding vectors (the style
    centroid) — mirrors ``app.render.pipeline._mean_vector`` /
    ``app.agents.critic._mean_vector`` (duplicated locally like those two
    already duplicate each other, rather than reaching into another module's
    private namespace)."""
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
class LiveEventShotRenderer:
    """Renders one :class:`EventShot` via the real ``Generator`` + the Critic gate.

    Satisfies :class:`~app.render.event_director.EventShotRenderer` structurally
    (Python protocols are structural — no explicit inheritance needed, matching
    how ``KenBurnsEventRenderer`` itself is defined).

    ``max_retries`` is the **total** number of render attempts allowed before
    degrading to Ken-Burns (attempt 1 counts toward this total) — note this is
    a different convention from :attr:`RenderPipeline.retry_cap`, which counts
    *extra* attempts beyond the first.
    """

    generator: ClipGenerator
    critic: ClipCritic
    #: EventScript.scene_id — an EventShot itself doesn't carry it (thread it
    #: from the caller, e.g. the worker dispatching a whole EventScript).
    scene_id: str | None = None
    #: Needed for DefectRepo.log's required book_id.
    book_id: str = ""
    #: The CanonService.query seam (None is valid for unit tests that don't
    #: exercise the canon-backed branch — see ``_canon_slice_for``).
    canon: CanonReader | None = None
    #: The same BlobStore given to ``EventDirector(store=...)``. ``None`` keeps
    #: the identity (CCS) check inert (``Critic._ccs``'s own documented
    #: fallback) — wire it for parity with RenderPipeline's per-shot gate.
    object_store: BlobStore | None = None
    #: ``providers.embeddings``, mirroring ``RenderPipeline``'s own ``embedder``
    #: field. ``None`` keeps the style-drift check inert (``Critic._style_drift``'s
    #: own documented fallback) even when ``object_store`` is wired.
    embedder: Embedder | None = None
    #: None is valid for unit tests that don't exercise the arbitration branch.
    defect_repo: DefectOps | None = None
    #: The same BudgetService RenderPipeline uses (worker.py's own
    #: _budget_factory(db)). ``None`` preserves this adapter's original,
    #: unmetered behaviour (existing unit tests construct it without one) —
    #: wiring it is what makes event-granularity renders show up in the
    #: Scheduler's own video-seconds ledger and respect its live-gate/low-
    #: buffer checks at all, exactly like the shot-granularity path already
    #: does (found missing entirely by an independent resilience audit: real
    #: Wan/MiniMax seconds were being spent with zero budget accounting).
    budget: BudgetOps | None = None
    #: RenderPipeline's own fallback voice id (build_render_pipeline's default).
    default_voice: str = "Cherry"
    max_retries: int = 2
    #: The vertical film geometry every shot renders at (kinora.md §4.2) — must
    #: match whatever EventDirector itself uses so a degraded shot's geometry
    #: doesn't clash with its live-rendered siblings in the final stitch.
    film_size: tuple[int, int] = FILM_SIZE
    fps: int = DEFAULT_FPS

    async def render_shot(
        self, shot: EventShot, *, still: bytes | None, audio: bytes | None
    ) -> RenderedShot:
        started = time.monotonic()
        spec = ShotSpec(
            shot_id=shot.shot_id,
            beat_id=shot.beat_id,
            scene_id=self.scene_id,
            render_mode=shot.render_mode,
            prompt=shot.summary,
            camera=shot.camera,
            target_duration_s=shot.duration_s,
            # reference_image_ids / end_frame_ref: no equivalent source on an
            # EventShot — see the module docstring's disclosed simplifications.
        )
        canon_slice = await self._canon_slice_for(shot)
        voice_id = self._voice_id(canon_slice)
        narration_text = (shot.summary or "").strip()
        # §9.5 identity/style gates: real signal when object_store/embedder are
        # wired, the Critic's own documented "gate inert" fallback (None)
        # otherwise — mirrors RenderPipeline._render_live_loop's own
        # locked_ref/style_centroid resolution (computed once, not per retry,
        # since canon_slice doesn't change across attempts either).
        locked_ref_image = await self._first_locked_ref_bytes(canon_slice)
        scene_style_centroid = await self._scene_style_centroid(canon_slice)

        # Budget-aware degradation: mirrors RenderPipeline._render_shot's own
        # pre-loop gate (pipeline.py, "Budget-aware degradation" comment) —
        # when unwired (None) this is a no-op, preserving the adapter's
        # original unmetered behaviour for callers that don't pass a budget.
        if self.budget is not None and (
            not self.budget.can_render_live() or await self.budget.is_low()
        ):
            attempt = self.max_retries  # skip the loop straight to degrade below
        else:
            attempt = 0
        while attempt < self.max_retries:
            attempt += 1
            retries_exhausted = attempt == self.max_retries

            reservation = None
            if self.budget is not None:
                try:
                    reservation = await self.budget.reserve(
                        shot.duration_s,
                        scene_id=self.scene_id,
                        book_id=self.book_id,
                        note=f"event render {shot.shot_id} attempt {attempt}",
                    )
                except BudgetExceeded:
                    logger.warning("live_event_shot.budget_exceeded", shot_id=shot.shot_id)
                    break

            try:
                output = await self.generator.render(
                    spec,
                    narration_text=narration_text,
                    voice_id=voice_id,
                    reference_image_bytes=[still] if still else None,
                    # Not available under EventDirector's concurrent fan-out —
                    # see the module docstring's disclosed simplifications.
                    prev_last_frame_bytes=None,
                )
            except (LiveVideoDisabled, ProviderError) as exc:
                # Mirrors RenderPipeline._render_live_loop's own
                # except (LiveVideoDisabled, ProviderError) around this exact
                # call: a provider outage is a non-retryable outcome for this
                # attempt — degrade this shot rather than let the exception
                # propagate out of EventDirector.render_event's bare
                # asyncio.gather and crash the whole event.
                if self.budget is not None and reservation is not None:
                    await self.budget.release(reservation)
                logger.warning(
                    "live_event_shot.generator_unavailable",
                    shot_id=shot.shot_id,
                    error=str(exc),
                )
                break
            except Exception:
                # An unclassified exception (independent review finding,
                # 2026-07-05): still release the outstanding reservation
                # before propagating — EventDirector.render_event's own
                # return_exceptions=True catches this at the gather level and
                # degrades just this shot, but without this release the
                # reservation would otherwise leak (never committed, never
                # released), permanently eroding the budget toward is_low().
                if self.budget is not None and reservation is not None:
                    await self.budget.release(reservation)
                raise
            actual = float(output.duration_s or shot.duration_s)
            if self.budget is not None and reservation is not None:
                await self.budget.commit(reservation, actual)
            frames = await self._frames(output)
            try:
                qa = await self.critic.score(
                    shot_id=shot.shot_id,
                    clip_frames=frames,
                    canon_slice=canon_slice,
                    character_crop=frames[0] if frames else None,
                    locked_ref_image=locked_ref_image,
                    scene_style_centroid=scene_style_centroid,
                    retries_exhausted=retries_exhausted,
                )
            except (LiveVideoDisabled, ProviderError) as exc:
                # Same non-retryable treatment as above — the Critic itself is
                # unavailable, so an unverified clip must not ship (mirrors
                # RenderPipeline's own except (LiveVideoDisabled, ProviderError)
                # around Critic.score).
                logger.warning(
                    "live_event_shot.critic_unavailable", shot_id=shot.shot_id, error=str(exc)
                )
                break
            if qa.verdict == Verdict.PASS:
                finished = time.monotonic()
                return RenderedShot(
                    shot_id=shot.shot_id,
                    ordinal=shot.ordinal,
                    clip_bytes=output.clip_bytes or b"",
                    last_frame_bytes=output.last_frame_bytes,
                    duration_s=float(output.duration_s or shot.duration_s),
                    render_mode=shot.render_mode,
                    word_timestamps=list(output.word_timestamps),
                    started_at=started,
                    finished_at=finished,
                )

            action = qa.repair_action
            if action in _NEEDS_ARBITRATION:
                if self.defect_repo is not None:
                    await self.defect_repo.log(
                        book_id=self.book_id,
                        kind="event_shot_needs_arbitration",
                        shot_id=shot.shot_id,
                        detail={"repair_action": action.value},
                    )
                break  # un-retryable outcome: fall through to degrade, no retry burned
            if action not in _RETRYABLE:
                break  # DEGRADE (or anything else unexpected): stop retrying

        # Retry cap exhausted, or a non-retryable outcome: degrade rather than
        # ship an unverified clip (mirrors RenderPipeline's own degrade-on-
        # exhaustion behaviour), using the exact same real rung
        # KenBurnsEventRenderer uses for the off-gate path.
        finished = time.monotonic()
        if still is not None:
            clip = await self._ken_burns(still, audio, shot)
            last_frame: bytes | None = still
        else:
            clip = await self._audio_text_card(audio, shot)
            last_frame = None
        return RenderedShot(
            shot_id=shot.shot_id,
            ordinal=shot.ordinal,
            clip_bytes=clip,
            last_frame_bytes=last_frame,
            duration_s=shot.duration_s,
            render_mode=shot.render_mode,
            started_at=started,
            finished_at=finished,
            degraded=True,
        )

    # -- argument sourcing, mirroring RenderPipeline's own helpers ----------- #

    async def _canon_slice_for(self, shot: EventShot) -> CanonSlice:
        """The real CanonSlice when a canon reader + beat_id are available.

        Mirrors ``RenderPipeline._render_shot``'s ``canon_slice = await
        self._canon.query(book_id, beat_id)``. Falls back to an empty-but-real
        slice (never ``None``) when no canon reader is wired or the shot
        carries no ``beat_id`` (e.g. a packed multi-beat segment from
        ``plan_segment_script``) — ``Critic._vision`` reads
        ``canon_slice.active_states`` unconditionally, so ``None`` would crash
        the real Critic.
        """
        beat_id = shot.beat_id
        if self.canon is not None and beat_id is not None:
            return await self.canon.query(self.book_id, beat_id)
        return CanonSlice(
            book_id=self.book_id, beat_id=beat_id or shot.shot_id, beat_index=shot.ordinal
        )

    def _voice_id(self, canon_slice: CanonSlice) -> str:
        """Mirrors ``RenderPipeline._voice_id``: the first locked character's voice."""
        for character in canon_slice.characters:
            if character.voice:
                vid = character.voice.get("cosyvoice_voice_id") or character.voice.get("voice_id")
                if isinstance(vid, str) and vid:
                    return vid
        return self.default_voice

    @staticmethod
    def _entities(canon_slice: CanonSlice) -> list[CanonEntitySlice]:
        """Mirrors ``RenderPipeline._entities``: every entity on the slice that
        can carry reference images (characters + the active location + props)."""
        loc = [canon_slice.location] if canon_slice.location is not None else []
        return [*canon_slice.characters, *loc, *canon_slice.props]

    async def _first_locked_ref_bytes(self, canon_slice: CanonSlice) -> bytes | None:
        """Mirrors ``RenderPipeline._first_locked_ref_bytes``: the first present,
        locked reference image across the slice's entities — the real identity
        anchor ``Critic._ccs`` measures the clip against. ``None`` when no
        ``object_store`` is wired (the documented gate-inert fallback) or no
        locked reference is present in storage yet (e.g. an unestablished
        character)."""
        store = self.object_store
        if store is None:
            return None
        for entity in self._entities(canon_slice):
            for ref in entity.reference_images:
                if ref.locked and ref.key and await anyio.to_thread.run_sync(store.exists, ref.key):
                    return await anyio.to_thread.run_sync(store.get_bytes, ref.key)
        return None

    async def _scene_style_centroid(self, canon_slice: CanonSlice) -> list[float] | None:
        """Mirrors ``RenderPipeline._scene_style_centroid``: the mean embedding of
        the scene Style node's locked reference image(s) — the real anchor
        ``Critic._style_drift`` measures the clip's style against. ``None`` only
        when there is no ``object_store``/``embedder`` wired, no Style node on
        the slice, or no present locked style reference — the same residual
        cases ``RenderPipeline`` itself falls back for; otherwise the style gate
        is live."""
        embedder = self.embedder
        store = self.object_store
        if embedder is None or store is None or canon_slice.style is None:
            return None
        images: list[bytes] = []
        for ref in canon_slice.style.reference_images:
            if ref.locked and ref.key and await anyio.to_thread.run_sync(store.exists, ref.key):
                images.append(await anyio.to_thread.run_sync(store.get_bytes, ref.key))
        if not images:
            return None
        vectors = await embedder.embed_images(images)
        return _mean_vector(vectors) if vectors else None

    async def _frames(self, output: GeneratorOutput) -> list[bytes]:
        """Mirrors ``RenderPipeline._frames``: real sampled clip frames for the
        Critic — never the raw clip bytes (``clip_frames`` is NOT
        ``[output.clip_bytes]``)."""
        if not output.clip_bytes:
            return []
        try:
            return await anyio.to_thread.run_sync(extract_frames, output.clip_bytes, 4)
        except FfmpegError:
            return []

    # -- the degrade rung, mirroring KenBurnsEventRenderer exactly ----------- #

    async def _ken_burns(self, still: bytes, audio: bytes | None, shot: EventShot) -> bytes:
        zoom = zoom_for_camera(shot.camera)
        return await anyio.to_thread.run_sync(
            lambda: ken_burns_over_image(
                still,
                shot.duration_s,
                audio_bytes=audio,
                size=self.film_size,
                fps=self.fps,
                zoom_max=zoom,
            )
        )

    async def _audio_text_card(self, audio: bytes | None, shot: EventShot) -> bytes:
        return await anyio.to_thread.run_sync(
            lambda: audio_text_card(
                shot.duration_s, audio_bytes=audio, size=self.film_size, fps=self.fps
            )
        )


__all__ = ["LiveEventShotRenderer"]
