"""The render engine facade — resumable, observable, poison-safe (kinora.md §9.7).

:class:`app.render.pipeline.RenderPipeline` is the §9.7 per-shot orchestrator. The
engine wraps it with the hardening layers built in this domain so the worker (or a
backfill command) gets resumability, telemetry, and poison-quarantine *without the
pipeline changing*:

* **resume / skip** — before running a shot, probe its :class:`CheckpointStore`;
  a terminal checkpoint short-circuits to a no-op (a re-claim after the ack/clear
  race never re-renders), and a mid-flight one resumes;
* **checkpoint after** — once the pipeline returns, persist a terminal checkpoint
  so a re-claim is idempotent, then clear it (the shot is done);
* **poison quarantine** — a shot that has crashed the renderer past the threshold
  is *not* handed to the pipeline at all; the engine ships the guaranteed bottom
  rung directly (the pipeline still owns the clean degrade ladder for QA fails);
* **telemetry** — every resume/skip/checkpoint/poison decision is a §12.5 event.

It also exposes :meth:`render_scene`, which builds a §9.3/§9.6 dependency DAG over
a scene's shots and runs it with bounded parallelism (continuation chains stay
ordered, independents fan out) — the deterministic batch scheduler from
:mod:`app.render.dag`.

The engine speaks the pipeline's ``render_shot`` signature, so a worker swaps it
in transparently. It is constructed against a narrow ``ShotRenderer`` Protocol
(the pipeline satisfies it), so the engine's control flow is unit-testable with a
light fake renderer — no ffmpeg/DB/network needed for the hardening logic.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from app.agents.contracts import DirectorNote
from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.db.models.enums import ShotStatus
from app.render.checkpoint import (
    CheckpointStore,
    InMemoryCheckpointStore,
    ShotCheckpoint,
    probe_resume,
)
from app.render.dag import RenderGraph, build_scene_graph, run_graph
from app.render.ladder import LadderReason, Rung
from app.render.pipeline import RenderResult
from app.render.poison import InMemoryPoisonStore, PoisonTracker
from app.render.states import RenderState
from app.render.telemetry import RenderEvent, TelemetryBus, recording_bus

logger = get_logger("app.render.engine")


class ShotRenderer(Protocol):
    """The slice of :class:`RenderPipeline` the engine drives (it satisfies this)."""

    async def render_shot(
        self,
        book_id: str,
        shot_id: str,
        *,
        session_id: str | None = None,
        director_notes: list[DirectorNote] | None = None,
        director_present: bool = False,
    ) -> RenderResult: ...


#: How the engine ships a poison-quarantined shot when it won't touch the pipeline.
#: Injected so a real wiring can write a degraded audio-card mp4; the default is a
#: pure result (the engine stays IO-free for unit tests).
PoisonDegrader = Callable[[str, str], Awaitable[RenderResult]]


def _default_poison_result(book_id: str, shot_id: str) -> RenderResult:
    """A pure, IO-free degraded result for a quarantined shot (bottom rung)."""
    return RenderResult(
        shot_id=shot_id,
        status=ShotStatus.DEGRADED,
        rung=Rung.AUDIO_TEXT_ONLY.value,
        video_seconds=0.0,
    )


class RenderEngine:
    """Hardened, resumable, observable wrapper over a §9.7 :class:`ShotRenderer`."""

    def __init__(
        self,
        pipeline: ShotRenderer,
        *,
        checkpoints: CheckpointStore | None = None,
        poison: PoisonTracker | None = None,
        bus: TelemetryBus | None = None,
        settings: Settings | None = None,
        poison_degrader: PoisonDegrader | None = None,
    ) -> None:
        self._pipeline = pipeline
        self._settings = settings or get_settings()
        self._checkpoint_enabled = self._settings.render_checkpoint_enabled
        self._checkpoints = checkpoints or InMemoryCheckpointStore()
        self._bus = bus or recording_bus()[0]
        self._poison = poison or PoisonTracker(
            store=InMemoryPoisonStore(),
            threshold=self._settings.render_poison_threshold,
            bus=self._bus,
        )
        self._poison_degrader = poison_degrader
        self._max_parallel = self._settings.render_max_parallel_shots

    @property
    def bus(self) -> TelemetryBus:
        """The telemetry bus (a worker can attach its own sinks)."""
        return self._bus

    @property
    def poison(self) -> PoisonTracker:
        """The poison tracker (shared so a worker can inspect quarantines)."""
        return self._poison

    async def render_shot(
        self,
        book_id: str,
        shot_id: str,
        *,
        session_id: str | None = None,
        director_notes: list[DirectorNote] | None = None,
        director_present: bool = False,
    ) -> RenderResult:
        """Render one shot through the pipeline with resume + poison + telemetry.

        Drop-in for :meth:`RenderPipeline.render_shot`. A quarantined shot is
        shipped at the bottom rung without touching the pipeline; otherwise the
        engine probes for a resumable checkpoint (a terminal one short-circuits),
        runs the pipeline, records the outcome to the poison tracker, and
        checkpoints the terminal state before clearing it (idempotent re-claim).
        """
        # 1) Poison gate: a crash-loop shot never reaches the (expensive) pipeline.
        if self._poison.is_poisoned(shot_id):
            logger.warning("engine.poison_skip", shot_id=shot_id)
            return await self._ship_poison(book_id, shot_id)

        # 2) Resume gate: a finished shot (terminal checkpoint) is a no-op.
        if self._checkpoint_enabled:
            decision = await probe_resume(self._checkpoints, shot_id)
            if decision.skip:
                self._bus.publish(RenderEvent.resumed(shot_id, RenderState.ACCEPTED, attempt=0))
                logger.info("engine.resume_skip", shot_id=shot_id)
                return self._checkpoint_to_result(book_id, shot_id, decision.checkpoint)
            if decision.checkpoint is not None:
                self._bus.publish(
                    RenderEvent.resumed(
                        shot_id, decision.checkpoint.state, attempt=decision.checkpoint.attempts
                    )
                )

        # 3) Run the real §9.7 pipeline.
        try:
            result = await self._pipeline.render_shot(
                book_id,
                shot_id,
                session_id=session_id,
                director_notes=director_notes,
                director_present=director_present,
            )
        except Exception as exc:  # a hard crash — feed the poison tracker + re-raise
            self._poison.record_failure(shot_id, exc)
            logger.warning("engine.render_crash", shot_id=shot_id, error=str(exc))
            raise

        # 4) Record the outcome + checkpoint the terminal state.
        if result.status is ShotStatus.ACCEPTED:
            self._poison.record_success(shot_id)
        await self._checkpoint_terminal(book_id, shot_id, result)
        return result

    # -- scene-level: dependency-ordered parallel render --------------------- #

    async def render_scene(
        self,
        book_id: str,
        shots: list[dict[str, Any]],
        *,
        session_id: str | None = None,
        max_parallel: int | None = None,
    ) -> dict[str, RenderResult]:
        """Render a whole scene as a §9.3/§9.6 DAG (parallel, dependency-ordered).

        ``shots`` are scene rows (``{shot_id, render_mode?, scene_id?, depends_on?}``)
        in reading order; :func:`build_scene_graph` wires the continuation chain so
        a ``video_continuation`` shot waits for its predecessor's accepted endpoint.
        Independent shots fan out up to ``max_parallel`` (default the configured
        cap). Returns each shot's :class:`RenderResult`. A continuation shot whose
        predecessor only degraded is shipped at the bottom rung (no endpoint to
        extend) rather than blocked silently.
        """
        graph: RenderGraph = build_scene_graph(shots)
        results: dict[str, RenderResult] = {}

        async def runner(shot_id: str) -> ShotStatus:
            result = await self.render_shot(book_id, shot_id, session_id=session_id)
            results[shot_id] = result
            return result.status

        report = await run_graph(
            graph, runner, max_parallel=max_parallel or self._max_parallel
        )
        # A continuation shot blocked on a degraded predecessor still ships a clip.
        for shot_id in report.blocked:
            results[shot_id] = await self._ship_poison(book_id, shot_id)
        logger.info(
            "engine.scene_done",
            book_id=book_id,
            shots=len(results),
            batches=report.batch_count,
            blocked=len(report.blocked),
        )
        return results

    # -- helpers ------------------------------------------------------------- #

    async def _ship_poison(self, book_id: str, shot_id: str) -> RenderResult:
        if self._poison_degrader is not None:
            return await self._poison_degrader(book_id, shot_id)
        self._bus.publish(
            RenderEvent.rung_selected(
                shot_id, Rung.AUDIO_TEXT_ONLY, reason=LadderReason.POISONED.value
            )
        )
        return _default_poison_result(book_id, shot_id)

    async def _checkpoint_terminal(
        self, book_id: str, shot_id: str, result: RenderResult
    ) -> None:
        if not self._checkpoint_enabled:
            return
        state = self._result_state(result)
        rung = self._result_rung(result)
        checkpoint = ShotCheckpoint(
            shot_id=shot_id,
            book_id=book_id,
            state=state,
            attempts=result.attempts,
            spent_video_seconds=result.video_seconds,
            last_rung=rung,
        )
        await self._checkpoints.save(checkpoint)
        self._bus.publish(RenderEvent.checkpointed(shot_id, state, attempt=result.attempts))
        # A done shot's checkpoint is immediately cleared: it has served its purpose
        # (a re-claim races against the clear; either the terminal checkpoint or the
        # absence of one both yield an idempotent no-op / fresh start).
        if state in (RenderState.ACCEPTED, RenderState.DEGRADED):
            await self._checkpoints.clear(shot_id)

    def _checkpoint_to_result(
        self, book_id: str, shot_id: str, checkpoint: ShotCheckpoint | None
    ) -> RenderResult:
        """Reconstruct a minimal terminal result from a resumed checkpoint."""
        rung = (checkpoint.last_rung.value if checkpoint and checkpoint.last_rung else "cache_hit")
        status = (
            ShotStatus.ACCEPTED
            if checkpoint is None or checkpoint.state is RenderState.ACCEPTED
            else ShotStatus.DEGRADED
        )
        return RenderResult(
            shot_id=shot_id,
            status=status,
            rung=rung,
            video_seconds=0.0,  # a resume re-charges nothing (the work was done)
            cache_hit=True,
        )

    @staticmethod
    def _result_state(result: RenderResult) -> RenderState:
        if result.status is ShotStatus.ACCEPTED:
            return RenderState.ACCEPTED
        if result.status is ShotStatus.CONFLICT:
            return RenderState.CONFLICT
        return RenderState.DEGRADED

    @staticmethod
    def _result_rung(result: RenderResult) -> Rung | None:
        mapping = {
            "full_video": Rung.FULL_WAN,
            "cache_hit": Rung.FULL_WAN,
            Rung.KEN_BURNS_KEYFRAME.value: Rung.KEN_BURNS_KEYFRAME,
            Rung.KEN_BURNS_ILLUSTRATION.value: Rung.KEN_BURNS_ILLUSTRATION,
            Rung.AUDIO_TEXT_ONLY.value: Rung.AUDIO_TEXT_ONLY,
        }
        return mapping.get(result.rung)


def build_render_engine(
    session: Any,
    *,
    providers: Any,
    object_store: Any,
    settings: Settings | None = None,
    checkpoints: CheckpointStore | None = None,
    poison: PoisonTracker | None = None,
    bus: TelemetryBus | None = None,
    default_voice: str = "Cherry",
    url_ttl: int = 3600,
) -> RenderEngine:
    """Wire a production :class:`RenderEngine` over the real §9.7 pipeline.

    Constructs the wired :class:`RenderPipeline` (via ``build_render_pipeline``)
    and wraps it with the hardening layers. A worker opts in *without changing* by
    passing ``run_shot=engine.render_shot`` to :class:`RenderWorker` (the engine
    speaks the pipeline's ``render_shot`` signature). The metrics + log sinks are
    attached so the additive §12.5 series flow in production; production should
    pass durable ``checkpoints`` / ``poison`` stores (Redis/DB) — the in-memory
    defaults keep a single worker correct but don't survive a restart.
    """
    from app.render.pipeline import build_render_pipeline
    from app.render.telemetry import LogSink, MetricsSink

    settings = settings or get_settings()
    pipeline = build_render_pipeline(
        session,
        providers=providers,
        object_store=object_store,
        settings=settings,
        default_voice=default_voice,
        url_ttl=url_ttl,
    )
    if bus is None:
        bus, _ = recording_bus()
    bus.add_sink(MetricsSink())
    bus.add_sink(LogSink())
    return RenderEngine(
        pipeline,
        checkpoints=checkpoints,
        poison=poison,
        bus=bus,
        settings=settings,
    )


__all__ = [
    "PoisonDegrader",
    "RenderEngine",
    "ShotRenderer",
    "build_render_engine",
]
