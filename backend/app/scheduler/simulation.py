"""Deterministic reading-trace replay harness (kinora.md §4.3–§4.10, §13).

The existing :func:`app.eval.buffer_trace.simulate_buffer_trace` proves the §4.10
sawtooth for *one* idealised reader (constant velocity, no pauses, no seeks). This
module generalises that into a full **simulation harness**: a deterministic engine
that replays an arbitrary *reading trace* — a scripted sequence of reader actions
(advance at velocity, dwell, pause, seek backward/forward) — through the **real**
:class:`~app.scheduler.service.SchedulerService` control loop and the real
:class:`~app.scheduler.prediction.ReadingModel`, sampling buffer health at every
tick. No infra, no video, no clock: a seed in, the same sawtooth out.

Why this exists
---------------
Every later subsystem in this domain — budget-optimal scheduling, multi-reader
fairness, adaptive watermarks, the A/B policy framework — is only as trustworthy
as the harness that scores it. So the harness is the load-bearing first piece: it
turns "the scheduler behaves well" from a claim into a number (§13 buffer health,
stall count, would-be video-seconds) computed offline, repeatably, for *any*
reader archetype.

Design
------
* **Reader archetypes** (:class:`ReaderProfile`) are pure generators of a
  :class:`ReadingTrace` — a list of :class:`ReaderAction`. ``steady`` reads at a
  fixed velocity; ``variable`` jitters velocity deterministically from a seed;
  ``skimmer`` rides above the clamp ceiling; ``thinker`` interleaves long dwell
  pauses; ``seeker`` jumps. All seeded → reproducible.
* **The engine** (:func:`replay_trace`) advances simulated wall-clock action by
  action, feeds each settled position into the real scheduler tick *and* the
  prediction model, honours idle-pause (a real pause yields no ticks) and seeks
  (via the real :class:`~app.scheduler.intent.IntentController` semantics, but
  inline so the harness stays infra-free), and records a :class:`BufferSample`
  per tick.
* **The result** (:class:`SimulationResult`) carries the sawtooth samples (so the
  existing :func:`app.eval.metrics.buffer_health` scores it unchanged), the
  prediction-model end state, and the **provably-zero** real video spend.

Zero spend, unchanged gate
--------------------------
The engine reuses the §4.4 dry-run collaborators (:class:`DryRunBudget`,
:class:`RecordingQueue`, :class:`RecordingKeyframes`) from
:mod:`app.eval.buffer_trace`, so ``video_seconds_spent`` and
``video_reservations_s`` are ``0.0`` by construction and promotion stays gated on
``budget.can_render_live()`` exactly as in production. A simulation can never
spend a credit.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum

from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.eval.buffer_trace import DryRunBudget, RecordingKeyframes, RecordingQueue
from app.eval.metrics import BufferHealth, BufferSample, buffer_health
from app.scheduler.model import SchedulerSession
from app.scheduler.prediction import ReadingModel
from app.scheduler.service import SchedulerService, ShotSource
from app.scheduler.zones import DEFAULT_VELOCITY_WPS, clamp_velocity, eta_seconds

logger = get_logger("app.scheduler.simulation")

#: Default settle cadence for a replay tick (the §4.7 debounce window, seconds).
DEFAULT_TICK_S = 2.5
#: Idle-pause threshold mirrored from §4.7 (ms) — a pause longer than this yields
#: a single idle tick (speculation halts) and then no ticks until motion resumes.
SIM_IDLE_PAUSE_MS = 8_000


class ActionKind(StrEnum):
    """The reader actions a :class:`ReadingTrace` can script (§4.3/§4.7/§4.8)."""

    #: Read forward at ``velocity_wps`` for ``duration_s`` of wall-clock.
    READ = "read"
    #: Hold position (dwell / think) for ``duration_s`` — no forward motion.
    PAUSE = "pause"
    #: Jump the focus playhead to ``target_word`` instantly (§4.8 seek).
    SEEK = "seek"


@dataclass(frozen=True, slots=True)
class ReaderAction:
    """One scripted segment of a reading trace.

    For ``READ``: advance at ``velocity_wps`` for ``duration_s``. For ``PAUSE``:
    hold for ``duration_s`` (an idle pause if it exceeds the idle threshold). For
    ``SEEK``: jump to ``target_word`` (``duration_s`` ignored).
    """

    kind: ActionKind
    duration_s: float = 0.0
    velocity_wps: float = DEFAULT_VELOCITY_WPS
    target_word: int | None = None


@dataclass(frozen=True, slots=True)
class ReadingTrace:
    """A deterministic script of reader actions starting at ``focus_word`` (§4.3)."""

    actions: list[ReaderAction]
    focus_word: int = 0
    label: str = "trace"

    @property
    def nominal_velocity_wps(self) -> float:
        """The trace's representative read velocity (first READ, else default)."""
        for action in self.actions:
            if action.kind is ActionKind.READ:
                return action.velocity_wps
        return DEFAULT_VELOCITY_WPS


class ReaderProfile:
    """Pure, seeded generators of canonical reader archetypes (§4.11).

    These are the rows of the §4.11 failure-mode table turned into reproducible
    traces: a steady reader, a velocity-varying reader, a skimmer that blows the
    clamp ceiling, a thinker that interleaves long dwell pauses, and a seeker that
    jumps. Every generator is deterministic given its arguments (and ``seed`` for
    the stochastic ones), so a regression in buffer health is reproducible.
    """

    @staticmethod
    def steady(
        *, velocity_wps: float = DEFAULT_VELOCITY_WPS, duration_s: float = 180.0
    ) -> ReadingTrace:
        """A reader at one fixed velocity — the §4.10 worked-example reader."""
        return ReadingTrace(
            actions=[ReaderAction(ActionKind.READ, duration_s, velocity_wps)],
            label=f"steady@{velocity_wps:g}wps",
        )

    @staticmethod
    def variable(
        *,
        base_wps: float = DEFAULT_VELOCITY_WPS,
        jitter: float = 0.4,
        segments: int = 12,
        segment_s: float = 15.0,
        seed: int = 0,
    ) -> ReadingTrace:
        """A reader whose velocity jitters ±``jitter`` fraction each segment.

        Deterministic from ``seed`` via a small integer LCG (no ``random`` import
        — keeps the harness importable with zero side effects). The jitter widens
        the velocity *variance* the prediction model and adaptive watermarks key
        off, without ever needing a real clock.
        """
        state = (seed * 1_103_515_245 + 12_345) & 0x7FFFFFFF
        actions: list[ReaderAction] = []
        for _ in range(segments):
            state = (state * 1_103_515_245 + 12_345) & 0x7FFFFFFF
            frac = (state / 0x7FFFFFFF) * 2.0 - 1.0  # in [-1, 1]
            v = max(0.5, base_wps * (1.0 + jitter * frac))
            actions.append(ReaderAction(ActionKind.READ, segment_s, v))
        return ReadingTrace(actions=actions, label=f"variable@{base_wps:g}±{jitter:g}")

    @staticmethod
    def skimmer(
        *, velocity_wps: float = 16.0, duration_s: float = 60.0
    ) -> ReadingTrace:
        """A skimmer above the §4.3 clamp ceiling — §4.6 suspends promotion."""
        return ReadingTrace(
            actions=[ReaderAction(ActionKind.READ, duration_s, velocity_wps)],
            label=f"skimmer@{velocity_wps:g}wps",
        )

    @staticmethod
    def thinker(
        *,
        velocity_wps: float = 3.0,
        read_s: float = 30.0,
        pause_s: float = 20.0,
        cycles: int = 4,
    ) -> ReadingTrace:
        """Read-then-think cycles: long pauses trigger §4.7 idle, frozen buffer."""
        actions: list[ReaderAction] = []
        for _ in range(cycles):
            actions.append(ReaderAction(ActionKind.READ, read_s, velocity_wps))
            actions.append(ReaderAction(ActionKind.PAUSE, pause_s))
        return ReadingTrace(actions=actions, label="thinker")

    @staticmethod
    def seeker(
        *,
        velocity_wps: float = 4.0,
        read_s: float = 30.0,
        jumps: Iterable[int] = (4000, 200, 6000),
    ) -> ReadingTrace:
        """Read, then jump far (§4.8) — exercises cancel + re-seed + bridge."""
        actions: list[ReaderAction] = []
        for target in jumps:
            actions.append(ReaderAction(ActionKind.READ, read_s, velocity_wps))
            actions.append(ReaderAction(ActionKind.SEEK, target_word=target))
        actions.append(ReaderAction(ActionKind.READ, read_s, velocity_wps))
        return ReadingTrace(actions=actions, label="seeker")


@dataclass(slots=True)
class SimulationResult:
    """The outcome of replaying one :class:`ReadingTrace` (§4.10/§13).

    ``samples`` is the buffer-occupancy sawtooth (scored by
    :func:`app.eval.metrics.buffer_health`); ``model`` is the prediction-model end
    state (its learned velocity/variance/dwell); the ``video_*`` fields are
    **always 0.0** (zero-spend proof); ``simulated_earmarks_s`` is the would-be
    committed video for reporting. ``seeks`` / ``idle_ticks`` count the §4.8/§4.7
    events the trace exercised.
    """

    label: str
    samples: list[BufferSample] = field(default_factory=list)
    model: ReadingModel = field(default_factory=ReadingModel)
    low: float = 0.0
    high: float = 0.0
    commit_horizon: float = 0.0
    video_seconds_spent: float = 0.0
    video_reservations_s: float = 0.0
    simulated_earmarks_s: float = 0.0
    committed_promotions: int = 0
    keyframes_ensured: int = 0
    seeks: int = 0
    idle_ticks: int = 0

    def health(self, *, low_watermark: float | None = None) -> BufferHealth:
        """Score the sawtooth with the §13 buffer-health metric."""
        return buffer_health(self.samples, low_watermark=low_watermark or self.low)

    def to_contract(self) -> list[dict[str, float]]:
        """The ``GET /api/eval/buffer-trace`` array shape (BufferSample items)."""
        return [s.to_contract() for s in self.samples]


async def replay_trace(
    trace: ReadingTrace,
    *,
    shots: ShotSource,
    book_id: str,
    settings: Settings | None = None,
    tick_s: float = DEFAULT_TICK_S,
    keyframe_cap: int = 12,
    session_id: str | None = None,
    seek_keep_threshold_s: float = 120.0,
    model: ReadingModel | None = None,
) -> SimulationResult:
    """Replay ``trace`` through the real scheduler + prediction model (zero video).

    Advances a simulated wall-clock action by action. A ``READ`` action steps the
    focus word at its velocity every ``tick_s``, runs one real
    :meth:`SchedulerService.on_event`, and folds the (words, dt) into ``model``. A
    ``PAUSE`` advances the clock without moving ``w``; if it exceeds the idle
    threshold the scheduler idle-pauses (one idle tick, then silence). A ``SEEK``
    re-seeds the focus playhead, drops far cached committed shots, resets the
    velocity to default, and continues — mirroring §4.8 inline so the harness
    needs no Redis-backed :class:`IntentController`.

    Returns a :class:`SimulationResult` whose video spend is provably ``0.0``.
    """
    settings = settings or get_settings()
    budget = DryRunBudget()
    queue = RecordingQueue()
    keyframes = RecordingKeyframes()
    service = SchedulerService(
        queue=queue,
        budget=budget,
        shots=shots,
        keyframes=keyframes,
        store=None,
        settings=settings,
        keyframe_cap=keyframe_cap,
    )
    low, high, commit_horizon, _spec = service.watermarks
    model = model or ReadingModel.with_halflives()

    session = SchedulerSession(
        session_id=session_id or f"sim_{trace.label}",
        book_id=book_id,
        focus_word=trace.focus_word,
        velocity_wps=clamp_velocity(trace.nominal_velocity_wps),
        raw_velocity_wps=abs(trace.nominal_velocity_wps),
    )

    samples: list[BufferSample] = []
    clock_ms = 0
    seeks = 0
    idle_ticks = 0
    last_intent_ms = 0
    session.last_activity_ms = clock_ms

    def _record(t_ms: int) -> None:
        samples.append(
            BufferSample(
                t=round(t_ms / 1000.0, 6),
                committed_seconds_ahead=session.committed_seconds_ahead,
                low=low,
                high=high,
            )
        )

    # Seed sample at t=0 (an initial tick fills toward H from a cold buffer).
    await service.on_event(session, allow_promotion=True, now_ms=clock_ms)
    _record(clock_ms)

    for action in trace.actions:
        if action.kind is ActionKind.SEEK and action.target_word is not None:
            seeks += 1
            clock_ms += int(tick_s * 1000)
            session.focus_word = action.target_word
            session.trajectory_token = session.trajectory_token  # kept; cancel is no-op here
            session.velocity_wps = DEFAULT_VELOCITY_WPS
            session.raw_velocity_wps = DEFAULT_VELOCITY_WPS
            session.bursting = False
            session.last_activity_ms = clock_ms
            last_intent_ms = clock_ms
            # §4.8: keep cached committed shots near the new position; drop the rest.
            session.committed_buffer = [
                bs
                for bs in session.committed_buffer
                if abs(
                    eta_seconds(bs.word_index_start, action.target_word, DEFAULT_VELOCITY_WPS)
                )
                <= seek_keep_threshold_s
            ]
            session.speculative_beats = []
            session.recompute_committed_ahead()
            await service.on_event(session, allow_promotion=True, now_ms=clock_ms)
            _record(clock_ms)
            continue

        n_ticks = max(1, int(round(action.duration_s / tick_s)))
        for _ in range(n_ticks):
            clock_ms += int(tick_s * 1000)
            if action.kind is ActionKind.PAUSE:
                # No motion. last_activity_ms is *not* refreshed → idle eventually.
                tick = await service.on_event(
                    session, allow_promotion=False, now_ms=clock_ms
                )
                if tick.idle:
                    idle_ticks += 1
                model.observe(words_advanced=0, dt_ms=clock_ms - last_intent_ms)
                last_intent_ms = clock_ms
                _record(clock_ms)
                continue

            # READ: advance the focus word at this action's velocity.
            words = int(round(action.velocity_wps * tick_s))
            prev_word = session.focus_word
            session.focus_word += words
            session.velocity_wps = clamp_velocity(action.velocity_wps)
            session.raw_velocity_wps = abs(action.velocity_wps)
            session.last_activity_ms = clock_ms
            await service.on_event(session, allow_promotion=True, now_ms=clock_ms)
            model.observe(
                words_advanced=session.focus_word - prev_word,
                dt_ms=clock_ms - last_intent_ms,
            )
            last_intent_ms = clock_ms
            _record(clock_ms)

    logger.info(
        "sim.replay",
        label=trace.label,
        book_id=book_id,
        ticks=len(samples),
        peak=max((s.committed_seconds_ahead for s in samples), default=0.0),
        seeks=seeks,
        idle_ticks=idle_ticks,
        video_seconds_spent=0.0,
    )
    return SimulationResult(
        label=trace.label,
        samples=samples,
        model=model,
        low=low,
        high=high,
        commit_horizon=commit_horizon,
        video_seconds_spent=0.0,
        video_reservations_s=budget.video_reserved,
        simulated_earmarks_s=budget.simulated_earmarks_s,
        committed_promotions=len(queue.committed_enqueues),
        keyframes_ensured=len(keyframes.ensured),
        seeks=seeks,
        idle_ticks=idle_ticks,
    )


__all__ = [
    "DEFAULT_TICK_S",
    "SIM_IDLE_PAUSE_MS",
    "ActionKind",
    "ReaderAction",
    "ReaderProfile",
    "ReadingTrace",
    "SimulationResult",
    "replay_trace",
]
