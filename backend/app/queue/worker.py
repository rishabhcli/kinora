"""The render worker — claim, render, publish, ack/retry/DLQ (kinora.md §12.1).

An async consumer of :class:`RedisRenderQueue`. Per the design's concurrency
lanes (§4.9/§12.2) it runs dedicated pools — **4 committed + 2 speculative + a
small keyframe pool** — so committed work is never starved by speculation. For
each claimed job it:

1. **checks the cancel token at a safe point** (before any provider call); if the
   trajectory was cancelled it releases the reserved budget earmark and finalizes
   the job as cancelled — no video-seconds are spent (§4.8);
2. otherwise hands budget accounting to the pipeline (releasing the Scheduler's
   gating earmark, which the pipeline re-reserves authoritatively) and runs the
   real :meth:`RenderPipeline.render_shot`;
3. on success publishes a ``clip_ready`` event on the session's pub/sub channel
   (§5.6) and acks; on a transient failure it backs off and retries; once the
   retry cap is exhausted the job dead-letters (the pipeline has itself already
   degraded the shot to the Ken-Burns rung, so the film never hard-stops).

Keyframe-lane jobs run the cheap image lane (:class:`KeyframeService`) which
spends **zero** video-seconds.

Run it as a process with ``python -m app.queue.worker``.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
from collections.abc import Awaitable, Callable, Sequence
from contextlib import AbstractAsyncContextManager
from typing import TYPE_CHECKING, Any

from app.core.config import Settings, get_settings
from app.core.logging import configure_logging, get_logger
from app.db.models.enums import RenderPriority, ShotStatus
from app.memory.budget_service import Reservation
from app.memory.conflict_log import record_conflict_history
from app.queue.redis_queue import (
    LANE_ORDER,
    QueuedJob,
    RedisRenderQueue,
    book_channel,
    conflict_object_key,
    session_channel,
)
from app.render.pipeline import BudgetOps, ClipCritic, RenderResult, UnknownShotError

if TYPE_CHECKING:
    from app.render.event_director import EventRenderResult
    from app.render.stitch import StitchResult

logger = get_logger("app.queue.worker")

#: A shot is "terminal" (no longer fillable) once accepted or degraded; a scene
#: is stitched when every one of its shots is terminal (§9.6).
_TERMINAL_SHOT = frozenset({ShotStatus.ACCEPTED, ShotStatus.DEGRADED})

# A claimed shot render returns the pipeline's structured result.
ShotRunner = Callable[[QueuedJob], Awaitable[RenderResult]]
# A keyframe job returns anything truthy (its result is published by the service).
KeyframeRunner = Callable[[QueuedJob], Awaitable[Any]]
SessionFactory = Callable[[], AbstractAsyncContextManager[Any]]

# Errors that can never succeed on retry — dead-letter immediately.
_PERMANENT = (UnknownShotError,)


#: Builds the real ``BudgetService`` bound to a db session (``_default_budget_factory``);
#: typed against the full ``BudgetOps`` seam (not just the ``release`` slice
#: ``_release_earmark`` uses) so ``_run_event_job`` can also wire it into
#: ``LiveEventShotRenderer`` for reserve/commit/gate parity with the
#: shot-granularity path's own ``build_render_pipeline`` wiring.
BudgetFactory = Callable[[Any], BudgetOps]


class RenderWorker:
    """Drains the priority queue and runs the real per-shot render pipeline."""

    def __init__(
        self,
        queue: RedisRenderQueue,
        redis: Any,
        *,
        settings: Settings | None = None,
        run_shot: ShotRunner | None = None,
        run_keyframe: KeyframeRunner | None = None,
        budget_factory: BudgetFactory | None = None,
        session_factory: SessionFactory | None = None,
        providers: Any | None = None,
        object_store: Any | None = None,
        critic: ClipCritic | None = None,
        poll_interval_s: float = 0.25,
        lease_heartbeat_s: float = 30.0,
    ) -> None:
        self._queue = queue
        self._redis = redis
        self._settings = settings or get_settings()
        self._run_shot = run_shot
        self._run_keyframe = run_keyframe
        self._budget_factory = budget_factory or self._default_budget_factory
        self._session_factory = session_factory
        self._providers = providers
        self._object_store = object_store
        # Injectable for tests / callers that already have a calibrated Critic;
        # the event-granularity path (_run_event_job) otherwise builds one the
        # same way build_render_pipeline does for the shot-granularity path.
        self._critic = critic
        self._poll = poll_interval_s
        # Cadence the worker re-extends a job's lease at while it actively renders;
        # well under the queue lease so a slow render is never reaped (§12.1).
        self._lease_heartbeat_s = lease_heartbeat_s

    @property
    def queue(self) -> RedisRenderQueue:
        """The backing queue (handy for tests/inspection)."""
        return self._queue

    # -- single-job processing ---------------------------------------------- #

    async def process_once(
        self, *, lanes: Sequence[RenderPriority] | None = None, now_ms: int | None = None
    ) -> bool:
        """Claim and process one job; returns False when nothing was ready."""
        job = await self._queue.claim(lanes=lanes, now_ms=now_ms)
        if job is None:
            return False
        await self.process_job(job)
        return True

    async def process_job(self, job: QueuedJob) -> None:
        """Process a single claimed job through its terminal state."""
        # Safe point: a trajectory the reader moved away from is cancelled here,
        # before any provider work — releasing the reserved budget (§4.8/§12.1).
        if job.cancelled or await self._queue.is_cancelled(job.id):
            await self._release_earmark(job, reason="cancelled")
            await self._queue.finalize_cancelled(job.id)
            logger.info("worker.cancelled", job_id=job.id, shot_hash=job.shot_hash)
            return

        if job.priority is RenderPriority.KEYFRAME:
            await self._process_keyframe(job)
            return

        await self._process_render(job)

    async def _process_render(self, job: QueuedJob) -> None:
        if job.shot_id is None:
            await self._queue.retry(job.id, error="job has no shot_id")
            return

        await self._queue.mark_submitted(job.id)
        # Hand authoritative budget accounting to the pipeline: release the
        # Scheduler's gating earmark; the pipeline reserves + commits the real
        # spend (§11.1). This keeps the ledger from double-counting.
        await self._release_earmark(job, reason="handoff")

        runner = self._run_shot or self._default_run_shot
        # §5.4: surface the Cinematographer composing this shot ahead of the
        # reader, so the feed shows the crew planning — not just the clip arriving.
        if job.shot_id:
            await self._publish_agent_activity(
                job,
                agent="cinematographer",
                message=f"Composing shot {job.shot_id}",
                shot_id=job.shot_id,
            )
        try:
            result = await self._run_with_lease_heartbeat(job, runner)
        except _PERMANENT as exc:
            logger.warning("worker.permanent_failure", job_id=job.id, error=str(exc))
            await self._queue.retry(job.id, error=str(exc), now_ms=self._far_future())
            return
        except Exception as exc:  # transient: back off + retry, then DLQ
            outcome = await self._queue.retry(job.id, error=str(exc))
            logger.warning(
                "worker.render_error",
                job_id=job.id,
                error=str(exc),
                decision=outcome.decision.value,
                attempts=outcome.attempts,
            )
            return

        await self._publish_render_events(job, result)
        await self._queue.ack(job.id)
        logger.info(
            "worker.render_done",
            job_id=job.id,
            shot_id=result.shot_id,
            status=result.status.value,
            rung=result.rung,
            video_seconds=result.video_seconds,
        )
        # §9.6: once this shot completes its scene, stitch the accepted clips and
        # publish ``scene_stitched`` (absolute-time sync map). Best-effort — a
        # stitch failure must never undo the ack or fail the render. Skipped for
        # an event job (Task 9): its shots already share ONE merged/stitched
        # clip, so SceneStitcher's one-clip-per-shot concat would splice N
        # copies of that same event clip back-to-back instead of the scene.
        if not job.shot_ids:
            await self._maybe_stitch_scene(job, result)

    async def _run_with_lease_heartbeat(self, job: QueuedJob, runner: ShotRunner) -> RenderResult:
        """Run the render while heartbeating its lease so the reaper can't steal it."""
        stop = asyncio.Event()
        beat = asyncio.create_task(self._lease_heartbeat(job.id, stop))
        try:
            return await runner(job)
        finally:
            stop.set()
            beat.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await beat

    async def _lease_heartbeat(self, job_id: str, stop: asyncio.Event) -> None:
        while not stop.is_set():
            await self._sleep_or_stop(stop, self._lease_heartbeat_s)
            if stop.is_set():
                break
            with contextlib.suppress(Exception):
                await self._queue.extend_lease(job_id)

    async def _process_keyframe(self, job: QueuedJob) -> None:
        runner = self._run_keyframe
        if runner is None:
            logger.warning("worker.keyframe_unconfigured", job_id=job.id)
            await self._queue.ack(job.id)
            return
        try:
            await runner(job)
        except Exception as exc:
            outcome = await self._queue.retry(job.id, error=str(exc))
            logger.warning(
                "worker.keyframe_error",
                job_id=job.id,
                error=str(exc),
                decision=outcome.decision.value,
            )
            return
        await self._queue.ack(job.id)
        logger.info("worker.keyframe_done", job_id=job.id, beat_id=job.beat_id)

    # -- budget earmark release --------------------------------------------- #

    async def _release_earmark(self, job: QueuedJob, *, reason: str) -> None:
        """Release the Scheduler's reserved video-seconds for ``job`` (idempotent)."""
        if not job.reservation_id or job.reserved_video_s <= 0:
            return
        if self._session_factory is None:
            return
        reservation = Reservation(
            id=job.reservation_id,
            video_seconds=job.reserved_video_s,
            book_id=job.book_id or None,
            session_id=job.session_id,
            scene_id=job.scene_id,
        )
        try:
            async with self._session_factory() as db:
                budget = self._budget_factory(db)
                await budget.release(reservation, note=f"{reason} {job.id}")
            logger.info(
                "worker.budget_released",
                job_id=job.id,
                reason=reason,
                video_seconds=job.reserved_video_s,
            )
        except Exception as exc:
            logger.warning("worker.budget_release_failed", job_id=job.id, error=str(exc))

    def _default_budget_factory(self, db: Any) -> BudgetOps:
        from app.db.repositories.budget import BudgetRepo
        from app.memory.budget_service import BudgetLimits, BudgetService

        return BudgetService(repo=BudgetRepo(db), limits=BudgetLimits.from_settings(self._settings))

    # -- default real pipeline runner --------------------------------------- #

    async def _default_run_shot(self, job: QueuedJob) -> RenderResult:
        if self._session_factory is None:
            raise RuntimeError("worker has no session_factory; cannot build the render pipeline")
        if self._providers is None or self._object_store is None:
            raise RuntimeError("worker missing providers/object_store; use build_worker()")
        async with self._session_factory() as db:
            if job.shot_ids:
                # Task 9: render_granularity="event" grouped this job's shots
                # into ONE merged clip at Scheduler-promotion time.
                return await self._run_event_job(job, db)

            from app.render.pipeline import build_render_pipeline

            pipeline = build_render_pipeline(
                db,
                providers=self._providers,
                object_store=self._object_store,
                settings=self._settings,
            )
            assert job.shot_id is not None
            # A render tied to a live reading session has a director present, so a
            # canon violation surfaces for the reader to arbitrate (§7.2) rather
            # than silently auto-honouring; background/speculative renders (no
            # session) keep the safe auto-resolve default.
            return await pipeline.render_shot(
                job.book_id,
                job.shot_id,
                session_id=job.session_id,
                director_present=job.session_id is not None,
            )

    # -- event-granularity runner (Task 9) ----------------------------------- #

    async def _run_event_job(self, job: QueuedJob, db: Any) -> RenderResult:
        """Render ``job.shot_ids`` (a Scheduler-batched, same-scene group of
        already-planned ``shots`` rows) as ONE merged continuous clip.

        Rebuilds the beat cluster behind the batch (``BeatRepo.list_by_scene``,
        filtered/ordered to just this job's shots — a scene can hold more beats
        than fit in one ``MAX_EVENT_SHOTS``-capped batch), plans it with the
        existing ``plan_segment_script``, and renders it via ``EventDirector`` +
        ``LiveEventShotRenderer`` wired the same way ``build_render_pipeline``
        wires the shot-granularity path's own Generator/Critic. The merged
        clip's key and each shot's own ``[clip_start_s, clip_end_s)`` window
        (Task 8) are then fanned back onto every original shot row so the read
        path (``_shot_response``) can serve them.
        """
        from app.agents.critic import Critic
        from app.agents.generator import Generator
        from app.db.repositories.beat import BeatRepo
        from app.db.repositories.shot import ShotRepo
        from app.render.event_director import (
            EventDirector,
            plan_segment_script,
            shot_duration_for_beat,
        )
        from app.render.live_event_renderer import LiveEventShotRenderer
        from app.render.segment_packer import pack_segments

        assert job.shot_ids
        assert self._providers is not None  # _default_run_shot already checked this
        shots_repo = ShotRepo(db)
        beats_repo = BeatRepo(db)

        shot_rows = []
        for shot_id in job.shot_ids:
            row = await shots_repo.get(shot_id)
            if row is None:
                logger.warning("worker.event_shot_missing", job_id=job.id, shot_id=shot_id)
                continue
            shot_rows.append(row)
        if not shot_rows:
            raise UnknownShotError(f"none of event job {job.id}'s shots exist: {job.shot_ids}")

        scene_beats = await beats_repo.list_by_scene(job.scene_id) if job.scene_id else []
        beats_by_id = {beat.id: beat for beat in scene_beats}
        ordered_beats = [
            beats_by_id[row.beat_id] for row in shot_rows if row.beat_id in beats_by_id
        ]
        if not ordered_beats:
            raise UnknownShotError(
                f"no beats resolved for event job {job.id} (scene {job.scene_id})"
            )

        agent_beats = [_to_agent_beat(beat) for beat in ordered_beats]
        script = plan_segment_script(
            event_id=job.id, book_id=job.book_id, scene_id=job.scene_id, beats=agent_beats
        )
        # Re-derive the SAME (pure, deterministic) packing plan_segment_script
        # used internally, purely to recover which beats — hence which original
        # shots — landed in which segment: plan_segment_script's EventScript
        # only exposes each segment's FIRST beat_id, not pack_segments' full
        # per-segment beat_ids list.
        segments = pack_segments(
            agent_beats, duration_for_beat=shot_duration_for_beat, scene_id=job.scene_id
        )
        beat_to_segment = {
            beat_id: seg.segment_id for seg in segments for beat_id in seg.beat_ids
        }

        critic = self._critic or Critic(self._providers, settings=self._settings)
        generator = Generator(self._providers, video_backend=self._providers.video)
        # Same BudgetService build_render_pipeline wires for the shot-granularity
        # path — without it, event-granularity renders spend real provider
        # seconds with zero accounting against the Scheduler's ledger/live-gate
        # (resilience audit finding).
        budget = self._budget_factory(db)
        renderer = LiveEventShotRenderer(
            generator=generator,
            critic=critic,
            scene_id=job.scene_id,
            book_id=job.book_id,
            budget=budget,
        )
        director = EventDirector(renderer=renderer, store=self._object_store)
        result = await director.render_event(script)

        await self._persist_event_shots(shots_repo, shot_rows, beat_to_segment, result)
        return self._to_event_render_result(job, result)

    async def _persist_event_shots(
        self,
        shots_repo: Any,
        shot_rows: list[Any],
        beat_to_segment: dict[str, str],
        result: EventRenderResult,
    ) -> None:
        """Fan the merged clip's key + each shot's own offset window (Task 8)
        back onto every original shot row, and settle each row's status off ITS
        OWN segment's degrade flag (mirrors ``RenderPipeline._accept``/
        ``_degrade`` transitioning a shot to ``ACCEPTED``/``DEGRADED``) — never
        left at a pre-render status, or the Scheduler would keep re-offering an
        already-rendered shot forever.

        A row whose beat never resolved to a packed segment (defensive only —
        ``ordered_beats`` is built from these same rows, so every row's beat
        should always land in ``beat_to_segment``) is skipped entirely rather
        than marked accepted with a shared ``clip_key`` but no offset window,
        which a client would misread as "this shot IS the whole clip".
        """
        offsets = {
            seg.shot_id: (seg.video_start_s, seg.video_end_s) for seg in result.sync_map.segments
        }
        degraded_segments = {r.shot_id for r in result.rendered if r.degraded}

        for row in shot_rows:
            segment_id = beat_to_segment.get(row.beat_id or "")
            if segment_id is None:
                logger.warning(
                    "worker.event_shot_unresolved_segment", shot_id=row.id, beat_id=row.beat_id
                )
                continue
            fields: dict[str, Any] = {
                "output": {
                    "clip_key": result.clip_key,
                    # Not result.clip_url: that presigned URL's TTL may have
                    # lapsed by the time a client reads the shot list, and
                    # _shot_response() already re-presigns from clip_key.
                    "clip_url": None,
                    "last_frame_key": result.last_frame_keys.get(segment_id),
                }
            }
            window = offsets.get(segment_id)
            if window is not None:
                fields["clip_start_s"], fields["clip_end_s"] = window
            await shots_repo.update(row.id, **fields)
            if segment_id in degraded_segments:
                await shots_repo.set_status(row.id, ShotStatus.DEGRADED)
            else:
                await shots_repo.mark_accepted(row.id)

    def _to_event_render_result(self, job: QueuedJob, result: EventRenderResult) -> RenderResult:
        """Adapt ``EventDirector``'s ONE merged-clip result to this worker's
        per-job ``RenderResult`` shape (§5.6 ``clip_ready`` / logging) — a
        summary of the whole batch, not any one original shot (each shot's own
        outcome is what :meth:`_persist_event_shots` just wrote to its row)."""
        any_degraded = any(r.degraded for r in result.rendered)
        return RenderResult(
            shot_id=job.shot_id or result.event_id,
            status=ShotStatus.DEGRADED if any_degraded else ShotStatus.ACCEPTED,
            rung="event_degraded" if any_degraded else "full_video",
            clip_key=result.clip_key,
            clip_url=result.clip_url,
            last_frame_key=next(iter(result.last_frame_keys.values()), None),
            video_seconds=0.0 if any_degraded else result.duration_s,
            attempts=1,
        )

    # -- events -------------------------------------------------------------- #

    async def _publish_render_events(self, job: QueuedJob, result: RenderResult) -> None:
        """Fan out the §5.6 event(s) appropriate to this render outcome."""
        channel = session_channel(job.session_id) if job.session_id else book_channel(job.book_id)
        if result.status is ShotStatus.CONFLICT and result.conflict is not None:
            conflict = result.conflict.model_dump(mode="json")
            # Persist the structured conflict so the conflict_choice handler can
            # apply the Director's pick (regenerate the shot / evolve canon, §7.2),
            # and log it to the session's history so a refresh can reload it.
            if job.session_id:
                await self._redis.set_json(
                    conflict_object_key(job.session_id, result.conflict.conflict_id),
                    conflict,
                    ttl_s=86_400,
                )
                await record_conflict_history(
                    self._redis,
                    job.session_id,
                    conflict=conflict,
                    conflict_id=result.conflict.conflict_id,
                )
            await self._redis.publish(
                channel,
                {
                    "event": "conflict_choice",
                    "conflict_id": result.conflict.conflict_id,
                    "options": conflict.get("options", []),
                    "claim": result.conflict.claim,
                    "canon_fact": result.conflict.canon_fact,
                    "current_beat": result.conflict.current_beat,
                    "raised_by": result.conflict.raised_by,
                    "shot_id": result.shot_id,
                },
            )
            await self._redis.publish(
                channel,
                {
                    "event": "agent_activity",
                    "agent": result.conflict.raised_by or "Continuity",
                    "message": f"Continuity conflict: {result.conflict.claim}",
                    "conflict": conflict,
                    "shot_id": result.shot_id,
                },
            )
            logger.info(
                "worker.conflict_surfaced",
                job_id=job.id,
                shot_id=result.shot_id,
                conflict_id=result.conflict.conflict_id,
            )
            return

        # An auto-resolved conflict (honour/evolve, never surfaced): show the
        # Showrunner's decision record in the feed for §7.2 transparency.
        if result.decision is not None:
            await self._redis.publish(
                channel,
                {
                    "event": "agent_activity",
                    "agent": "showrunner",
                    "message": result.decision.get("reasoning")
                    or f"Resolved conflict: {result.decision.get('chosen_option')}",
                    "conflict": result.decision,
                    "shot_id": result.shot_id,
                },
            )

        if result.clip_url or result.clip_key:
            await self._publish_clip_ready(job, result)

    async def _publish_clip_ready(self, job: QueuedJob, result: RenderResult) -> None:
        channel = session_channel(job.session_id) if job.session_id else book_channel(job.book_id)
        payload = {
            "event": "clip_ready",
            "shot_id": result.shot_id,
            "clip_key": result.clip_key,
            "oss_url": result.clip_url,
            "sync_segment": result.sync_segment,
            "qa": result.qa,
            "rung": result.rung,
            "video_seconds": result.video_seconds,
        }
        await self._redis.publish(channel, payload)
        # §5.4: the feed shows the Generator producing the shot + the Critic's QA,
        # so the user can watch the crew render + score each clip, not just its arrival.
        # §12.4 ladder is visible here: a real Wan clip vs a cache reuse vs a
        # degraded bridge (Ken-Burns / audio-text) all read distinctly in the feed.
        rung = result.rung or ""
        if rung == "full_video":
            gen_msg = f"Rendered shot {result.shot_id}"
        elif rung == "cache_hit":
            gen_msg = f"Reused cached shot {result.shot_id}"
        else:
            gen_msg = f"Bridged shot {result.shot_id} — {rung} (ladder)"
        await self._publish_agent_activity(
            job,
            agent="generator",
            message=gen_msg,
            shot_id=result.shot_id,
        )
        qa = result.qa or {}
        if qa:
            ccs = qa.get("ccs")
            passed = str(qa.get("verdict", "")).lower() == "pass"
            detail = f" — CCS {float(ccs):.2f}" if isinstance(ccs, (int, float)) else ""
            await self._publish_agent_activity(
                job,
                agent="critic",
                aspect="qa",
                message=f"Shot {result.shot_id} {'passed' if passed else 'flagged in'} QA{detail}",
                shot_id=result.shot_id,
                qa=qa,
            )

    async def _publish_agent_activity(
        self,
        job: QueuedJob,
        *,
        agent: str,
        message: str,
        aspect: str | None = None,
        shot_id: str | None = None,
        qa: dict[str, Any] | None = None,
    ) -> None:
        """Publish one §5.4 crew activity to the reader's session feed (best-effort).

        Scoped to live sessions: background/speculative renders (no session) have
        no one watching, so we skip them rather than spam the book channel.
        """
        if not job.session_id:
            return
        payload: dict[str, Any] = {"event": "agent_activity", "agent": agent, "message": message}
        if aspect is not None:
            payload["aspect"] = aspect
        if shot_id is not None:
            payload["shot_id"] = shot_id
        if qa is not None:
            payload["qa"] = qa
        with contextlib.suppress(Exception):
            await self._redis.publish(session_channel(job.session_id), payload)

    # -- scene stitch + ship (§9.6) ------------------------------------------ #

    async def _maybe_stitch_scene(self, job: QueuedJob, result: RenderResult) -> None:
        """Stitch the scene + publish ``scene_stitched`` once the scene completes.

        Triggered when a shot reaches a terminal state: if every shot in the scene
        is now terminal (and at least one accepted), concat the accepted clips,
        merge the per-shot sync segments into one scene map in **absolute** video
        time (``merge_sync_segments``), and publish it. Best-effort and fully
        guarded so a stitch problem never breaks the just-acked render.
        """
        if self._object_store is None or self._session_factory is None:
            return  # stitching needs the DB + object store (wired by build_worker)
        scene_id = job.scene_id
        if not scene_id or result.status not in _TERMINAL_SHOT:
            return
        try:
            stitched = await self._stitch_scene_if_complete(scene_id)
        except Exception as exc:  # noqa: BLE001 - stitching must never fail the render
            logger.warning("worker.scene_stitch_failed", scene_id=scene_id, error=str(exc))
            return
        if stitched is not None:
            await self._publish_scene_stitched(job, stitched)

    async def _stitch_scene_if_complete(self, scene_id: str) -> StitchResult | None:
        from app.render.stitch import SceneStitcher

        assert self._session_factory is not None
        assert self._object_store is not None
        async with self._session_factory() as db:
            if not await self._scene_complete(db, scene_id):
                return None
            return await SceneStitcher(db, object_store=self._object_store).stitch_scene(scene_id)

    @staticmethod
    async def _scene_complete(db: Any, scene_id: str) -> bool:
        """True once every shot in the scene is terminal and at least one accepted."""
        from sqlalchemy import select

        from app.db.models.shot import Shot

        rows = list(
            (await db.execute(select(Shot.status).where(Shot.scene_id == scene_id))).scalars().all()
        )
        if not rows or any(status not in _TERMINAL_SHOT for status in rows):
            return False
        return any(status is ShotStatus.ACCEPTED for status in rows)

    async def _publish_scene_stitched(self, job: QueuedJob, stitched: StitchResult) -> None:
        channel = session_channel(job.session_id) if job.session_id else book_channel(job.book_id)
        await self._redis.publish(
            channel,
            {
                "event": "scene_stitched",
                "scene_id": stitched.scene_id,
                "oss_url": stitched.clip_url,
                "sync_map": stitched.sync_map.model_dump(mode="json"),
            },
        )
        logger.info(
            "worker.scene_stitched",
            scene_id=stitched.scene_id,
            shots=stitched.shot_count,
            duration_s=stitched.duration_s,
        )

    # -- run loop ------------------------------------------------------------ #

    async def run(self, *, stop: asyncio.Event | None = None) -> None:
        """Run the dedicated lane pools until ``stop`` is set."""
        stop = stop or asyncio.Event()
        lane_pool = {
            RenderPriority.COMMITTED: self._settings.concurrency_committed,
            RenderPriority.SPECULATIVE: self._settings.concurrency_speculative,
            RenderPriority.KEYFRAME: self._settings.concurrency_keyframe,
        }
        logger.info("worker.start", lanes={k.value: v for k, v in lane_pool.items()})
        async with asyncio.TaskGroup() as tg:
            for lane in LANE_ORDER:
                for _ in range(lane_pool[lane]):
                    tg.create_task(self._lane_loop(lane, stop))
            tg.create_task(self._reaper_loop(stop))
        logger.info("worker.stopped")

    async def _lane_loop(self, lane: RenderPriority, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                processed = await self.process_once(lanes=[lane])
            except Exception as exc:  # never let one job kill the lane loop
                logger.error("worker.lane_loop_error", lane=lane.value, error=str(exc))
                processed = False
            if not processed:
                await self._sleep_or_stop(stop, self._poll)

    async def _reaper_loop(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await self._queue.reap_expired()
                # Refresh the live queue-depth gauges off the same cadence (§12.5);
                # ``stats()`` updates the Prometheus gauge as a side effect.
                await self._queue.stats()
            except Exception as exc:
                logger.error("worker.reaper_error", error=str(exc))
            await self._sleep_or_stop(stop, 5.0)

    @staticmethod
    async def _sleep_or_stop(stop: asyncio.Event, timeout: float) -> None:
        try:
            await asyncio.wait_for(stop.wait(), timeout=timeout)
        except TimeoutError:
            return

    @staticmethod
    def _far_future() -> int:
        # Force immediate dead-letter on permanent errors by exhausting backoff.
        return 2**62


def _to_agent_beat(beat: Any) -> Any:
    """ORM ``Beat`` -> ``agents.contracts.Beat``, for ``_run_event_job``'s beat
    cluster (Task 9). Mirrors ``RenderPipeline._to_agent_beat`` exactly;
    duplicated locally per this codebase's existing convention for small
    cross-module conversions rather than reaching into another module's
    private method (cf. the three copies of ``_mean_vector`` already living in
    pipeline.py / critic.py / live_event_renderer.py). Local imports keep this
    module's own import graph cheap when the event path is never exercised.
    """
    from app.agents.contracts import Beat as AgentBeat
    from app.agents.contracts import SourceSpan

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


def _queue_backoff_from_settings(settings: Settings) -> Any | None:
    """Build a jittered :class:`BackoffSchedule` from settings, or None for fixed.

    ``queue_backoff_jitter == "none"`` (the default) returns None so the queue uses
    the literal ``queue_retry_backoff_s`` schedule — fully back-compatible. Any
    other strategy returns a seeded schedule so retries spread out (§12.1). The
    seed is fixed in non-prod for reproducible tests and left random in prod.
    """
    from app.queue.backoff import BackoffSchedule, JitterStrategy

    strategy = (settings.queue_backoff_jitter or "none").lower()
    if strategy == JitterStrategy.NONE.value:
        return None
    try:
        jitter = JitterStrategy(strategy)
    except ValueError:
        logger.warning("worker.bad_backoff_jitter", value=strategy)
        return None
    seed = None if settings.app_env == "prod" else 1337
    return BackoffSchedule(
        strategy=jitter,
        base_s=settings.queue_backoff_base_s,
        cap_s=settings.queue_backoff_cap_s,
        seed=seed,
    )


def build_worker(
    *,
    settings: Settings | None = None,
    redis: Any | None = None,
    session_factory: SessionFactory | None = None,
) -> RenderWorker:
    """Wire a production :class:`RenderWorker` against the real providers/stores."""
    from app.db.session import get_session
    from app.providers import create_providers
    from app.redis.client import RedisClient
    from app.scheduler.keyframe import KeyframeService
    from app.storage.object_store import ObjectStore

    settings = settings or get_settings()
    redis_client = redis or RedisClient.from_url(settings.redis_url)
    factory: SessionFactory = session_factory or get_session
    queue = RedisRenderQueue(
        redis_client,
        retry_cap=settings.retry_cap,
        retry_backoff_s=tuple(settings.queue_retry_backoff_s),
        backoff=_queue_backoff_from_settings(settings),
        backpressure_depth=settings.queue_backpressure_depth,
        session_factory=factory,
    )
    providers = create_providers(settings)
    object_store = ObjectStore.from_settings(settings)
    keyframe_service = KeyframeService(
        image=providers.image, object_store=object_store, redis=redis_client, settings=settings
    )

    return RenderWorker(
        queue,
        redis_client,
        settings=settings,
        session_factory=factory,
        providers=providers,
        object_store=object_store,
        run_keyframe=lambda job: keyframe_service.ensure_keyframe(
            job.book_id, job.beat_id or "", prompt=job.prompt, session_id=job.session_id
        ),
    )


def main() -> None:
    """``python -m app.queue.worker`` entrypoint: build real deps and run."""
    settings = get_settings()
    configure_logging(settings.log_level)

    async def _run() -> None:
        worker = build_worker(settings=settings)
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, stop.set)
        await worker.run(stop=stop)

    asyncio.run(_run())


if __name__ == "__main__":
    main()
