"""The end-to-end system under test: reading → scheduler → queue → render → events,
wired and run inside the deterministic sim (kinora.md §9.8 happy path, §12 the
unglamorous engineering).

This is the integration centerpiece. It assembles the *real* control-plane code —
:class:`~app.scheduler.service.SchedulerService` and
:class:`~app.queue.redis_queue.RedisRenderQueue` (over the faulting redis seam) —
against deterministic sim doubles for the seams a virtual-time run must not touch
(budget, source-span, keyframes, render), and drives the whole thing from a seeded
:class:`~app.verification.simulation.workload.ReaderModel` on the virtual clock.

The three planes of kinora.md §9.8, as discrete-event processes:

#. **Reader → Scheduler.** A reader intent (advance / dwell / idle / seek) is
   scheduled on the loop every settle window. Each one folds into the real
   :class:`~app.scheduler.model.SchedulerSession` and runs one real
   ``SchedulerService.on_event`` — promoting shots into the queue under the §4.5
   watermark hysteresis, gated by the §4.6 velocity-adaptive promotion.
#. **Queue → Worker → Render.** A pool of worker lanes, each a self-rescheduling
   event, drains the real queue: ``claim`` a job, model its render as a
   virtual-time latency (with injected stalls / crashes), drive the §9.7 state walk
   via the real :class:`~app.render.simulator.RenderSimulator`, then ``ack`` /
   ``retry`` / dead-letter through the real queue. Leases are reaped by a periodic
   :class:`~app.queue.leases.Reaper` tick so an orphaned (crashed/stalled) job
   recovers — the §12.1 lease-recovery path, under test.
#. **Render → Events.** Acceptance flips the scheduler's buffered shot to ``ready``
   and publishes ``clip_ready`` (and the scheduler's own ``buffer_state`` /
   ``budget_low``) through the capturing publisher, so the event trace the client
   would see is recorded on the timeline for the invariants to inspect.

Everything is deterministic given the run's fault schedule
(:class:`~app.verification.simulation.faults.FaultSchedule`): the reader, the
worker timing, the render outcomes, and every injected fault all draw from
seed-stable PRNG streams.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.core.config import Settings, get_settings
from app.db.models.enums import RenderPriority
from app.queue.fakeredis import FakeAsyncRedis, FakeRedisClient
from app.queue.leases import Reaper
from app.queue.redis_queue import QueuedJob, RedisRenderQueue
from app.render.simulator import RenderScenario, RenderSimulator
from app.render.states import RenderState
from app.scheduler.model import SchedulerSession
from app.scheduler.service import SchedulerService
from app.scheduler.zones import DEFAULT_VELOCITY_WPS, clamp_velocity, eta_seconds
from app.verification.simulation.buggify import Buggify
from app.verification.simulation.collaborators import (
    SimBudget,
    SimKeyframes,
    SimShotSource,
    build_book,
)
from app.verification.simulation.core import Prng
from app.verification.simulation.events import CapturingEventPublisher
from app.verification.simulation.faults import FaultKind
from app.verification.simulation.redis_sim import (
    FaultingRedis,
    SimRedisError,
    install_virtual_clock,
)
from app.verification.simulation.runtime import Simulation
from app.verification.simulation.workload import IntentKind, ReaderModel, make_reader

#: Transient seam failures the production callers retry rather than crash on (a
#: redis connection blip). The real control loop survives these — the API route
#: wrapping ``on_event`` and the worker lane loop wrapping ``claim`` both catch and
#: re-poll (``app.queue.worker``: "never let one job kill the lane loop"). The sim
#: must model that *outer resilience* via ``Simulation.run_resilient`` or a single
#: injected ``REDIS_ERROR`` would falsely look like a crash.
_TRANSIENT: tuple[type[BaseException], ...] = (SimRedisError, ConnectionError)

#: Nominal worker render latency band (ms) — Wan 2.7 is ~30–90s/clip (kinora.md §4.1).
_RENDER_BASE_MS = 30_000
_RENDER_SPAN_MS = 60_000
#: Keyframe render is image-gen — far cheaper.
_KEYFRAME_BASE_MS = 2_000
_KEYFRAME_SPAN_MS = 6_000
#: How often the lease reaper runs (ms) — recovers orphaned jobs.
_REAPER_INTERVAL_MS = 5_000
#: §4.7 idle-pause threshold mirrored into the reader→scheduler driver.
_IDLE_PAUSE_MS = 8_000


@dataclass(slots=True)
class SystemConfig:
    """Knobs for one simulated system (kept small; the seed does the variety)."""

    n_shots: int = 400
    shot_spacing_words: int = 12
    shot_duration_s: float = 5.0
    shots_per_scene: int = 6
    book_words: int = 6_000
    archetype: str = "steady"
    base_wps: float = 4.0
    n_committed_workers: int = 4
    n_speculative_workers: int = 2
    n_keyframe_workers: int = 2
    budget_total_s: float = 1_650.0
    budget_floor_s: float = 120.0
    session_duration_ms: int = 180_000  # a 3-minute reading session
    settle_ms: int = 2_500


@dataclass(slots=True)
class ShotRecord:
    """Per-shot lifecycle bookkeeping for the invariants (the source of truth).

    The simulation tracks every shot the scheduler promoted: when it was promoted,
    enqueued, how many render attempts, whether it reached a §9.7 terminal state,
    and how its budget reservation resolved. The no-stuck-shots and no-double-spend
    invariants read this.
    """

    shot_id: str
    job_id: str | None = None
    reservation_id: str | None = None
    reserved_s: float = 0.0
    promoted_at_ms: int = -1
    enqueued: bool = False
    attempts: int = 0
    terminal_state: RenderState | None = None
    accepted_at_ms: int = -1
    degraded: bool = False
    cancelled: bool = False
    committed_s: float = 0.0


@dataclass(slots=True)
class SystemReport:
    """Everything one run produced — the artifact the invariants score.

    Holds the full per-shot records, the buffer-occupancy samples (the §13
    sawtooth), the event trace, and references to the live components so the
    invariants can read final queue depths, DLQ length, and budget accounting.
    """

    config: SystemConfig
    shots: dict[str, ShotRecord] = field(default_factory=dict)
    buffer_samples: list[tuple[int, float]] = field(default_factory=list)  # (t_ms, committed_s)
    low_watermark: float = 25.0
    high_watermark: float = 75.0
    events: CapturingEventPublisher | None = None
    budget: SimBudget | None = None
    queue: RedisRenderQueue | None = None
    final_queue_depth: int = 0
    final_dlq_len: int = 0
    reaped_jobs: int = 0
    worker_crashes: int = 0
    ticks: int = 0

    def accepted_shots(self) -> list[ShotRecord]:
        return [s for s in self.shots.values() if s.terminal_state is RenderState.ACCEPTED]

    def degraded_shots(self) -> list[ShotRecord]:
        return [s for s in self.shots.values() if s.degraded]

    def unresolved_shots(self) -> list[ShotRecord]:
        """Promoted+enqueued shots that never reached a terminal §9.7 state."""
        return [
            s
            for s in self.shots.values()
            if s.enqueued and not s.cancelled and s.terminal_state is None
        ]


class SimulatedSystem:
    """Assemble and run one reading→scheduler→queue→render→events loop in the sim.

    Construct with a :class:`Simulation` (the seeded runtime) and a
    :class:`SystemConfig`; call :meth:`run` to drive a full session and return a
    :class:`SystemReport`. The system owns the real scheduler + queue and a set of
    virtual-time worker lanes; all timing and faults flow from the simulation's
    clock and Buggify.
    """

    def __init__(self, sim: Simulation, config: SystemConfig) -> None:
        self.sim = sim
        self.config = config
        self.report = SystemReport(config=config)

        bug: Buggify = sim.buggify

        # --- redis seam: real queue over a fault-injecting fake ------------- #
        inner = FakeAsyncRedis()
        install_virtual_clock(inner, sim.clock.as_callable_s())
        self._faulting = FaultingRedis(inner, bug, on_latency=sim.advance_clock)
        # The worker/client wrapper shares the *same* faulting raw client so the
        # queue and the worker-side helpers see one keyspace.
        self._client = FakeRedisClient(raw=self._faulting)  # type: ignore[arg-type]

        settings: Settings = get_settings()
        self.queue = RedisRenderQueue(
            self._client,
            backpressure_depth=64,
            retry_cap=settings.retry_cap,
            retry_backoff_s=tuple(settings.queue_retry_backoff_s),
            clock_ms=sim.clock.as_callable_ms(),
        )
        self.report.queue = self.queue

        # --- scheduler seams ------------------------------------------------ #
        self.budget = SimBudget(
            total_s=config.budget_total_s,
            floor_s=config.budget_floor_s,
            prng=sim.stream("budget"),
        )
        self.report.budget = self.budget
        self._book = build_book(
            config.n_shots,
            spacing=config.shot_spacing_words,
            duration_s=config.shot_duration_s,
            shots_per_scene=config.shots_per_scene,
        )
        self._shot_by_id = {s.id: s for s in self._book}
        self._shots = SimShotSource(self._book)
        self._keyframes = SimKeyframes()
        self.events = CapturingEventPublisher(sim.clock.as_callable_ms())
        self.report.events = self.events

        self.scheduler = SchedulerService(
            queue=self.queue,
            budget=self.budget,
            shots=self._shots,
            keyframes=self._keyframes,
            store=None,
            settings=settings,
            events=self.events,
            idle_pause_ms=_IDLE_PAUSE_MS,
        )
        low, high, _commit, _spec = self.scheduler.watermarks
        self.report.low_watermark = low
        self.report.high_watermark = high

        # --- session + reader ---------------------------------------------- #
        self.session = SchedulerSession(
            session_id=sim.prng.hexid("sess"),
            book_id="book_sim",
            focus_word=0,
            velocity_wps=clamp_velocity(config.base_wps),
            raw_velocity_wps=config.base_wps,
        )
        self.session.last_activity_ms = 0
        self._reader: ReaderModel = make_reader(
            sim.stream("reader"),
            config.archetype,
            book_words=config.book_words,
            base_wps=config.base_wps,
        )
        self._reader.settle_ms = config.settle_ms

        # --- render + worker timing ---------------------------------------- #
        self._render_sim = RenderSimulator()
        self._worker_prng: Prng = sim.stream("worker")
        self._reaper = Reaper(self.queue)
        #: Renders currently scheduled-to-complete (in flight on a virtual worker).
        #: Convergence = reader done AND queue empty AND nothing in flight.
        self._render_inflight = 0
        #: Set once the reader's session window has elapsed (no new intents).
        self._reader_done = False

    # ---------------------------------------------------------------------- #
    # Run
    # ---------------------------------------------------------------------- #

    def run(self) -> SystemReport:
        """Drive a full reading session to quiescence; return the report.

        Schedules the reader's intents and the worker/reaper loops on the virtual
        clock, drains to the session deadline, then lets in-flight renders + the
        reaper finish and heals partitions so the system can *converge* (the
        eventual-consistency invariant requires a quiescent end state).
        """
        cfg = self.config

        # Prime the buffer once at t=0 (a cold start fills toward H).
        self._tick(self.sim.now_ms, advance_word=0, velocity=cfg.base_wps, allow_promotion=True)

        # Schedule the reader's settled intents across the session.
        self._schedule_next_intent(self.sim.now_ms)

        # Start the worker lanes and the lease reaper.
        for lane, count in (
            (RenderPriority.COMMITTED, cfg.n_committed_workers),
            (RenderPriority.SPECULATIVE, cfg.n_speculative_workers),
            (RenderPriority.KEYFRAME, cfg.n_keyframe_workers),
        ):
            for w in range(count):
                self._arm_worker(lane, w, at_ms=self.sim.now_ms)
        self._arm_reaper(self.sim.now_ms)

        # Drive the reader session up to its deadline. Reader intents stop firing
        # after this, but worker + reaper events remain on the heap.
        deadline = cfg.session_duration_ms
        self.sim.run_until(deadline)
        self._reader_done = True

        # Convergence: the storm passes. A real outage clears in finite time, after
        # which a correct system must *heal* — drain the queue, reap orphans, settle
        # the ledger. We model that by quiescing fault injection (Buggify off) and
        # healing partitions, then draining the loop to idle. This is precisely the
        # premise of the eventual-consistency invariant: once faults stop, the
        # system reaches a clean, stuck-free terminal state. Without this a chaos
        # run could (correctly) churn forever and never let us assert convergence.
        self.sim.buggify.enabled = False
        self.sim.run_until_idle()

        self._finalize()
        return self.report

    def _should_keep_polling(self) -> bool:
        """Whether worker/reaper loops should re-arm.

        They keep polling while the reader is still active, or while there is any
        residual work to finish: a render still in flight, jobs queued (incl. ones
        sleeping on a retry backoff), or jobs still *leased* (a crashed/stalled
        worker's orphan that the reaper has not yet recovered). Once every one of
        those is quiet the loop drains to idle and the run converges — which is the
        quiescent end state the eventual-consistency invariant inspects.
        """
        if not self._reader_done:
            return True
        if self._render_inflight > 0:
            return True
        stats = self._q(lambda: self.queue.stats())
        if stats is None:
            # Could not read the queue through a blip: assume work may remain and
            # keep polling — convergence is only declared on a *confirmed* quiesce.
            return True
        return stats.total_queued > 0 or stats.processing > 0

    # ---------------------------------------------------------------------- #
    # Reader → Scheduler
    # ---------------------------------------------------------------------- #

    def _schedule_next_intent(self, now_ms: int) -> None:
        if now_ms >= self.config.session_duration_ms:
            return
        intent = self._reader.next_intent()
        fire_at = now_ms + intent.dt_ms

        def _apply(t_ms: int) -> None:
            self._apply_intent(t_ms, intent)
            self._schedule_next_intent(t_ms)

        self.sim.at(fire_at, _apply, label=f"intent:{intent.kind}")

    def _apply_intent(self, t_ms: int, intent: Any) -> None:
        session = self.session
        if intent.kind is IntentKind.SEEK and intent.target_word is not None:
            # §4.8 seek: re-seed playhead, drop far cached committed shots, reset v.
            session.focus_word = intent.target_word
            session.velocity_wps = DEFAULT_VELOCITY_WPS
            session.raw_velocity_wps = DEFAULT_VELOCITY_WPS
            session.bursting = False
            session.last_activity_ms = t_ms
            session.committed_buffer = [
                bs
                for bs in session.committed_buffer
                if abs(eta_seconds(bs.word_index_start, intent.target_word, DEFAULT_VELOCITY_WPS))
                <= 120.0
            ]
            session.recompute_committed_ahead()
            self._tick(t_ms, advance_word=0, velocity=DEFAULT_VELOCITY_WPS, allow_promotion=True)
            return

        if intent.kind in (IntentKind.IDLE, IntentKind.DWELL):
            # No motion. last_activity_ms is *not* refreshed → idle eventually trips.
            self._tick(t_ms, advance_word=0, velocity=session.velocity_wps, allow_promotion=False)
            return

        # ADVANCE: move the focus word, refresh activity, promote.
        session.focus_word += intent.words
        session.velocity_wps = clamp_velocity(intent.velocity_wps)
        session.raw_velocity_wps = abs(intent.velocity_wps)
        session.last_activity_ms = t_ms
        self._tick(
            t_ms, advance_word=intent.words, velocity=intent.velocity_wps, allow_promotion=True
        )

    def _tick(
        self, t_ms: int, *, advance_word: int, velocity: float, allow_promotion: bool
    ) -> None:
        """Run one real scheduler tick and record what it promoted.

        Run **once** (never retried): ``on_event`` is *not* idempotent — it mutates
        session state and reserves budget — so wholesale retry would double-promote.
        That mirrors production: the API route that calls ``on_event`` does not
        replay it on a transient broker error; it drops this tick and lets the next
        debounced intent recover (kinora.md §4.9 — the loop runs "on every intent
        update or job-completion event"). On a transient error we record buffer
        state and move on, exactly as the live control plane would.
        """
        self.report.ticks += 1
        try:
            tick = self.sim.run_sync(
                self.scheduler.on_event(
                    self.session, allow_promotion=allow_promotion, now_ms=t_ms
                )
            )
        except _TRANSIENT:
            self.report.buffer_samples.append((t_ms, self.session.committed_seconds_ahead))
            return
        for shot_id in tick.promoted:
            rec = self.report.shots.get(shot_id)
            if rec is None:
                rec = ShotRecord(shot_id=shot_id, promoted_at_ms=t_ms, enqueued=True)
                self.report.shots[shot_id] = rec
                shot = self._shot_by_id.get(shot_id)
                rec.reserved_s = (shot.duration_s if shot and shot.duration_s else 5.0)
        self.report.buffer_samples.append((t_ms, self.session.committed_seconds_ahead))

    # ---------------------------------------------------------------------- #
    # Queue → Worker → Render → Events
    # ---------------------------------------------------------------------- #

    def _record_for(self, job: QueuedJob) -> ShotRecord:
        """Get-or-create the authoritative :class:`ShotRecord` for a job's shot.

        ``report.shots`` is keyed by ``shot_id`` and is the single source of truth
        for the per-shot invariants. We populate it lazily from the *job* (not the
        scheduler's ``tick.promoted``) so a shot is tracked even when the tick that
        promoted it was a no-op-return or its promotion was deduped — every job a
        worker handles maps to exactly one record, which is what the no-stuck and
        no-double-spend invariants require.
        """
        key = job.shot_id or job.id
        rec = self.report.shots.get(key)
        if rec is None:
            shot = self._shot_by_id.get(key)
            reserved = shot.duration_s if shot and shot.duration_s else job.target_duration_s
            rec = ShotRecord(
                shot_id=key,
                job_id=job.id,
                reservation_id=job.reservation_id,
                reserved_s=reserved or 5.0,
                enqueued=True,
            )
            self.report.shots[key] = rec
        return rec

    def _q(self, make_coro: Any, *, default: Any = None) -> Any:
        """Run a queue coroutine with the worker lane loop's transient resilience.

        Every queue op the worker performs goes through here so an injected
        ``REDIS_ERROR`` is retried (then dropped this cycle) rather than crashing
        the lane — faithfully modelling ``app.queue.worker``'s "never let one job
        kill the lane loop" guard. ``make_coro`` is a zero-arg factory (a coroutine
        is single-shot, so each retry needs a fresh one).
        """
        return self.sim.run_resilient(make_coro, transient=_TRANSIENT, default=default)

    def _arm_worker(self, lane: RenderPriority, worker_id: int, *, at_ms: int) -> None:
        """Schedule one worker lane to poll the queue at ``at_ms``."""

        def _poll(t_ms: int) -> None:
            self._worker_poll(lane, worker_id, t_ms)

        self.sim.at(at_ms, _poll, label=f"worker:{lane}:{worker_id}")

    def _worker_poll(self, lane: RenderPriority, worker_id: int, t_ms: int) -> None:
        """One worker poll: claim a job from the real queue and process it.

        If a job is claimed, model its render as a virtual-time latency: the worker
        is busy until ``t_ms + render_ms``, at which point :meth:`_complete_render`
        fires. If the queue is empty the worker re-arms after a short poll delay —
        but only while there is (or may yet be) work to do, so the loop converges.
        """
        job = self._q(lambda: self.queue.claim(lanes=[lane], now_ms=t_ms))
        if job is None:
            # Idle poll (empty lane *or* a transient blip): re-arm only while work
            # remains (else let the loop drain to a quiescent end).
            if self._should_keep_polling():
                self._arm_worker(lane, worker_id, at_ms=t_ms + 250)
            return

        rec = self._record_for(job)

        # Cooperative cancel (the §4.8 seek path): a cancelled job releases its
        # earmark and finalises without rendering.
        if job.cancelled or self._q(lambda: self.queue.is_cancelled(job.id), default=False):
            self._release_and_cancel(job, rec, t_ms)
            self._arm_worker(lane, worker_id, at_ms=t_ms)
            return

        # WORKER_CRASH: the worker dies mid-render. The job stays leased (orphaned)
        # and the reaper must recover it. The lane re-arms with a fresh "worker".
        if self.sim.buggify.should(FaultKind.WORKER_CRASH, "worker.crash", detail=job.id):
            self.report.worker_crashes += 1
            self._arm_worker(lane, worker_id, at_ms=t_ms + 250)
            return

        self._q(lambda: self.queue.mark_submitted(job.id))

        # Render latency, with an optional WORKER_STALL extending it past the lease.
        if lane is RenderPriority.KEYFRAME:
            render_ms = _KEYFRAME_BASE_MS + self._worker_prng.randint(0, _KEYFRAME_SPAN_MS)
        else:
            render_ms = _RENDER_BASE_MS + self._worker_prng.randint(0, _RENDER_SPAN_MS)
        stall = self.sim.buggify.duration(FaultKind.WORKER_STALL, "worker.stall", detail=job.id)
        render_ms += stall

        def _done(done_t: int) -> None:
            self._complete_render(lane, worker_id, job, done_t)

        self._render_inflight += 1
        self.sim.at(t_ms + render_ms, _done, label=f"render:{job.id}")

    def _complete_render(
        self, lane: RenderPriority, worker_id: int, job: QueuedJob, t_ms: int
    ) -> None:
        """Finish a render: drive the §9.7 walk, then ack / retry / dead-letter.

        If the worker stalled past the lease, the job may already have been reaped
        and re-claimed by another worker; we re-check the queue's live job to avoid
        double-acking (the at-least-once → idempotent-effect path).
        """
        self._render_inflight -= 1
        live = self._q(lambda: self.queue.get_job(job.id))
        if live is None:
            # Job already acked/dead-lettered/reaped-and-reclaimed: nothing to do.
            self._arm_worker(lane, worker_id, at_ms=t_ms)
            return

        rec = self._record_for(job)

        # Keyframe lane: cheap, (almost) always succeeds; no §9.7 video walk.
        if lane is RenderPriority.KEYFRAME:
            if self.sim.buggify.should(
                FaultKind.PROVIDER_TRANSIENT, "keyframe.transient", detail=job.id
            ):
                self._retry_or_deadletter(job, rec, t_ms, error="keyframe transient")
            else:
                self._q(lambda: self.queue.ack(job.id))
            self._arm_worker(lane, worker_id, at_ms=t_ms)
            return

        # Drive the real §9.7 state machine for this attempt's outcome.
        scenario = self._render_scenario(job)
        sim_report = self._render_sim.run(scenario)
        if rec is not None:
            rec.attempts += 1

        if sim_report.final_state is RenderState.ACCEPTED:
            self._accept(job, rec, sim_report, t_ms)
            self._q(lambda: self.queue.ack(job.id))
        elif sim_report.final_state is RenderState.DEGRADED:
            # Degraded is a *terminal success of the loop* (the film never hard-stops):
            # the shot lands on a cheaper rung. Ack it and release the earmark.
            self._degrade(job, rec, t_ms)
            self._q(lambda: self.queue.ack(job.id))
        else:
            # QA rejected / provider transient → queue-level retry (backoff or DLQ).
            self._retry_or_deadletter(job, rec, t_ms, error="qa/provider failure")

        self._arm_worker(lane, worker_id, at_ms=t_ms)

    def _render_scenario(self, job: QueuedJob) -> RenderScenario:
        """Build a §9.7 scenario for one render attempt from injected faults.

        Rolls the run's Buggify for a hard provider failure (forces the ladder), a
        transient (a retry), or a QA reject (the §9.5 repair loop). The
        :class:`RenderSimulator` then walks the real state machine to a terminal
        state; we read its verdict back.
        """
        from app.render.simulator import QAVerdict

        bug = self.sim.buggify
        if bug.should(FaultKind.PROVIDER_HARD_FAIL, "provider.hard_fail", detail=job.id):
            # Every attempt fails → retries exhausted → degrade to the ladder.
            return RenderScenario(
                shot_id=job.shot_id or job.id,
                live_feasible=True,
                qa_sequence=[
                    QAVerdict.identity_fail(),
                    QAVerdict.identity_fail(),
                    QAVerdict.identity_fail(),
                ],
                target_duration_s=job.target_duration_s,
            )
        if bug.should(FaultKind.QA_REJECT, "qa.reject", detail=job.id):
            # First attempt fails QA, repair succeeds → ACCEPTED after a retry.
            return RenderScenario(
                shot_id=job.shot_id or job.id,
                live_feasible=True,
                qa_sequence=[QAVerdict.style_fail(), QAVerdict.passing()],
                target_duration_s=job.target_duration_s,
            )
        # Clean render.
        return RenderScenario(
            shot_id=job.shot_id or job.id,
            live_feasible=True,
            qa_sequence=[QAVerdict.passing()],
            target_duration_s=job.target_duration_s,
        )

    # ---------------------------------------------------------------------- #
    # Outcome handlers
    # ---------------------------------------------------------------------- #

    def _accept(self, job: QueuedJob, rec: ShotRecord | None, sim_report: Any, t_ms: int) -> None:
        if rec is not None and rec.terminal_state is not None:
            return  # already terminal (duplicate completion) — idempotent
        committed = self.budget.commit(job.reservation_id) if job.reservation_id else 0.0
        if rec is not None:
            rec.terminal_state = RenderState.ACCEPTED
            rec.accepted_at_ms = t_ms
            rec.committed_s = committed
        # Flip the scheduler's buffered shot to ready + publish clip_ready (§9.8).
        self.session.mark_ready(job.shot_id or "")
        self.sim.run_sync(
            self.events.publish(
                self.session.session_id,
                {"type": "clip_ready", "shot_id": job.shot_id, "video_seconds": committed},
            )
        )

    def _degrade(self, job: QueuedJob, rec: ShotRecord | None, t_ms: int) -> None:
        if rec is not None and rec.terminal_state is not None:
            return
        # Degraded shots ride the ladder (no full video) → release the earmark.
        if job.reservation_id:
            from app.memory.budget_service import Reservation

            self.sim.run_sync(
                self.budget.release(
                    Reservation(
                        id=job.reservation_id,
                        video_seconds=rec.reserved_s if rec else 5.0,
                    )
                )
            )
        if rec is not None:
            rec.terminal_state = RenderState.DEGRADED
            rec.degraded = True

    def _retry_or_deadletter(
        self, job: QueuedJob, rec: ShotRecord | None, t_ms: int, *, error: str
    ) -> None:
        from app.queue.redis_queue import RetryDecision

        outcome = self._q(lambda: self.queue.retry(job.id, error=error, now_ms=t_ms))
        if outcome is None:
            # A broker blip outlasted retries on the *retry call itself*: the job
            # stays leased and the reaper will re-surface it — no shot is lost.
            return
        if outcome.decision is RetryDecision.DEADLETTER:
            # DLQ → the shot drops to degradation (§12.1): release the earmark and
            # mark it degraded (the pipeline never blocks on one bad shot).
            self._degrade(job, rec, t_ms)

    def _release_and_cancel(self, job: QueuedJob, rec: ShotRecord | None, t_ms: int) -> None:
        if job.reservation_id:
            from app.memory.budget_service import Reservation

            self.sim.run_sync(
                self.budget.release(
                    Reservation(
                        id=job.reservation_id,
                        video_seconds=rec.reserved_s if rec else 5.0,
                    )
                )
            )
        self._q(lambda: self.queue.finalize_cancelled(job.id))
        if rec is not None:
            rec.cancelled = True

    # ---------------------------------------------------------------------- #
    # Lease reaper
    # ---------------------------------------------------------------------- #

    def _arm_reaper(self, at_ms: int) -> None:
        def _reap(t_ms: int) -> None:
            reaped = self._q(lambda: self._reaper.run_once(now_ms=t_ms), default=0)
            self.report.reaped_jobs += reaped or 0
            if self._should_keep_polling():
                self._arm_reaper(t_ms + _REAPER_INTERVAL_MS)

        self.sim.at(at_ms, _reap, label="reaper")

    # ---------------------------------------------------------------------- #
    # Finalise
    # ---------------------------------------------------------------------- #

    def _finalize(self) -> None:
        # Finalisation reads must succeed against a quiesced (fault-free) end state;
        # retry through any last blip so the report's depths are accurate.
        self.report.final_queue_depth = self._q(lambda: self.queue.depth(), default=0) or 0
        self.report.final_dlq_len = self._q(lambda: self.queue.dlq_len(), default=0) or 0


def run_system(sim: Simulation, config: SystemConfig | None = None) -> SystemReport:
    """Convenience: build and run a :class:`SimulatedSystem`, returning its report.

    Wraps the run in :func:`~app.verification.simulation.determinism.deterministic_entropy`
    so the production code's ``uuid``/``random``/``time`` calls are seed-stable —
    the last mile of byte-identical replay.
    """
    from app.verification.simulation.determinism import deterministic_entropy

    config = config or SystemConfig()
    with deterministic_entropy(sim.stream("entropy"), now_ms=sim.clock.as_callable_ms()):
        system = SimulatedSystem(sim, config)
        return system.run()


__all__ = [
    "ShotRecord",
    "SimulatedSystem",
    "SystemConfig",
    "SystemReport",
    "run_system",
]
