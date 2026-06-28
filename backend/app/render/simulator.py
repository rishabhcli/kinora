"""A deterministic, zero-IO simulator of the §9.7 render control flow.

The real :class:`app.render.pipeline.RenderPipeline` needs ffmpeg, a DB, an object
store, and (for the live path) DashScope. That makes its *control flow* — the
§9.7 state walk, the §9.5 repair loop, the retry cap → ladder, conflict routing,
poison quarantine, and resume-from-checkpoint — expensive to exercise across many
scenarios. This simulator drives the *same* decision modules the pipeline uses
(:class:`app.render.states.ShotStateMachine`, :func:`app.render.retry.decide_retry`,
:func:`app.render.ladder.plan_ladder`, :class:`app.render.poison.PoisonTracker`)
over a **scripted scenario** and returns a :class:`SimReport` with the final
state, rung, attempts, video-seconds, and the full §12.5 event trace — with no
ffmpeg/DB/network.

It is the proof harness for the engine's control flow (every §9.7 edge is
reachable in a unit test) and powers a "what-if" panel for the demo: feed it a QA
verdict sequence + a budget/gate setting + an asset inventory and watch where a
shot would land. It is *not* a renderer — it produces no mp4; ``video_seconds`` is
the budget that *would* have been spent, computed from the scenario.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.agents.contracts import Verdict
from app.agents.critic import decide_qa
from app.core.logging import get_logger
from app.render.ladder import (
    LadderAssets,
    LadderPlan,
    LadderReason,
    Rung,
    plan_ladder,
)
from app.render.poison import PoisonRecord, PoisonTracker
from app.render.retry import RetryPolicy, RetryStep, decide_retry
from app.render.states import RenderState, ShotStateMachine
from app.render.telemetry import RenderEvent, TelemetryBus, recording_bus

logger = get_logger("app.render.simulator")


@dataclass(frozen=True, slots=True)
class QAVerdict:
    """One scripted Critic scorecard for a simulated attempt (drives ``decide_qa``)."""

    ccs: float = 0.95
    style_drift: float = 0.02
    timeline_ok: bool = True
    motion: float = 0.05
    #: Whether the source text supports a canon change (timeline-fail routing).
    textual_evolution_supported: bool = False

    @staticmethod
    def passing() -> QAVerdict:
        return QAVerdict()

    @staticmethod
    def identity_fail() -> QAVerdict:
        return QAVerdict(ccs=0.50)

    @staticmethod
    def style_fail() -> QAVerdict:
        return QAVerdict(style_drift=0.5)

    @staticmethod
    def motion_fail() -> QAVerdict:
        return QAVerdict(motion=0.9)

    @staticmethod
    def timeline_fail(*, supported: bool = False) -> QAVerdict:
        return QAVerdict(timeline_ok=False, textual_evolution_supported=supported)


@dataclass(frozen=True, slots=True)
class ConflictOutcome:
    """How the scripted §7.2 flow resolves a surfaced timeline conflict.

    ``action`` is one of ``"honor"`` / ``"evolve"`` / ``"accept"`` / ``"surface"``
    mirroring :class:`app.render.conflict.ConflictResolution.action` (honor/evolve
    regen, accept clears, surface parks the shot for the director).
    """

    action: str = "honor"


@dataclass(slots=True)
class RenderScenario:
    """A fully-scripted render situation (the only input to the simulator).

    Attributes:
        shot_id / book_id: identity (for the event trace).
        live_feasible: the live Wan gate + budget allow a real render.
        budget_low: remaining budget is below the §11 floor (forces a degrade).
        assets: the still/audio inventory the ladder planner reads.
        qa_sequence: one :class:`QAVerdict` per attempt; the last repeats if the
            loop runs longer than the script.
        conflict: how a timeline conflict resolves (when QA routes to one).
        target_duration_s: the shot duration (the video-seconds a live attempt
            charges).
        raise_on_attempt: zero-based attempt indices that *crash* (a hard failure
            feeding the poison tracker) instead of producing a QA verdict.
        already_poisoned: the shot enters quarantined (forced bottom rung).
    """

    shot_id: str = "shot_sim"
    book_id: str = "book_sim"
    live_feasible: bool = True
    budget_low: bool = False
    assets: LadderAssets = field(default_factory=lambda: LadderAssets(has_keyframe=True))
    qa_sequence: list[QAVerdict] = field(default_factory=lambda: [QAVerdict.passing()])
    conflict: ConflictOutcome = field(default_factory=ConflictOutcome)
    target_duration_s: float = 5.0
    raise_on_attempt: frozenset[int] = frozenset()
    already_poisoned: bool = False

    def qa_at(self, attempt: int) -> QAVerdict:
        """The scripted verdict for a zero-based attempt (last repeats)."""
        if not self.qa_sequence:
            return QAVerdict.passing()
        return self.qa_sequence[min(attempt, len(self.qa_sequence) - 1)]


@dataclass(slots=True)
class SimReport:
    """The outcome of simulating one shot's render control flow."""

    shot_id: str
    final_state: RenderState
    rung: Rung
    attempts: int
    video_seconds: float
    surfaced_conflict: bool
    poisoned: bool
    events: list[RenderEvent] = field(default_factory=list)

    @property
    def accepted(self) -> bool:
        return self.final_state is RenderState.ACCEPTED

    @property
    def degraded(self) -> bool:
        return self.final_state is RenderState.DEGRADED

    def state_path(self) -> list[RenderState]:
        """The ordered §9.7 states entered (from the event trace)."""
        from app.render.telemetry import EventKind

        return [e.state for e in self.events if e.kind is EventKind.STATE_ENTERED and e.state]


class RenderSimulator:
    """Drives the §9.7 control flow over a :class:`RenderScenario`, zero-IO.

    Reuses the *same* decision modules as the live pipeline so the simulated path
    is faithful: the §9.7 state machine validates every edge, ``decide_qa`` routes
    a failed clip, ``decide_retry`` enforces the cap → ladder, ``plan_ladder``
    picks the degradation rung, and the :class:`PoisonTracker` quarantines a
    crash-loop.
    """

    def __init__(
        self,
        *,
        policy: RetryPolicy | None = None,
        poison: PoisonTracker | None = None,
        bus: TelemetryBus | None = None,
    ) -> None:
        self._policy = policy or RetryPolicy()
        self._poison = poison or PoisonTracker()
        self._bus = bus

    def run(self, scenario: RenderScenario) -> SimReport:
        """Simulate the scenario to a terminal §9.7 state; return a report."""
        bus, recorder = (self._bus, None)
        if bus is None:
            bus, recorder = recording_bus()
        # Route the poison tracker's quarantine events onto this run's trace so a
        # ``poisoned`` event shows up in the SimReport (the tracker is otherwise
        # bus-less by default).
        if self._poison.bus is None:
            self._poison.bus = bus

        machine = ShotStateMachine(scenario.shot_id, state=RenderState.PROMOTED)
        self._enter(bus, machine, RenderState.CACHE_CHECK)

        if scenario.already_poisoned:
            self._poison.store.put(
                PoisonRecord(
                    shot_id=scenario.shot_id,
                    failures=self._poison.threshold,
                    quarantined=True,
                )
            )

        # A pre-quarantined shot skips straight to the forced bottom rung.
        forced = self._poison.quarantine_plan_input(scenario.shot_id)
        if forced is not None:
            rung, reason = forced
            return self._finish_degrade(
                bus, recorder, machine, scenario, rung=rung, reason=reason, attempts=0,
                video_seconds=0.0, poisoned=True,
            )

        # Budget/gate gate: live off or budget low → straight to the ladder.
        if not scenario.live_feasible or scenario.budget_low:
            reason = (
                LadderReason.LIVE_VIDEO_DISABLED
                if not scenario.live_feasible
                else LadderReason.BUDGET_LOW
            )
            self._enter(bus, machine, RenderState.RENDERING)
            plan = self._plan(bus, scenario, reason)
            return self._finish_degrade(
                bus, recorder, machine, scenario, rung=plan.selected, reason=reason,
                attempts=0, video_seconds=0.0, poisoned=False,
            )

        return self._live_loop(bus, recorder, machine, scenario)

    # -- the live loop (mirrors RenderPipeline._render_live_loop) ------------ #

    def _live_loop(
        self,
        bus: TelemetryBus,
        recorder: object,
        machine: ShotStateMachine,
        scenario: RenderScenario,
    ) -> SimReport:
        spent = 0.0
        for attempt in range(self._policy.max_attempts):
            self._enter(bus, machine, RenderState.RENDERING)

            # A scripted hard crash this attempt → poison tracker + transient route.
            if attempt in scenario.raise_on_attempt:
                self._poison.record_failure(scenario.shot_id, RuntimeError("sim_crash"))
                if self._poison.is_poisoned(scenario.shot_id):
                    return self._finish_degrade(
                        bus, recorder, machine, scenario, rung=Rung.AUDIO_TEXT_ONLY,
                        reason=LadderReason.POISONED, attempts=attempt + 1,
                        video_seconds=spent, poisoned=True,
                    )
                # Not yet poisoned: treat as a transient failure → next attempt.
                if attempt >= self._policy.cap:
                    return self._finish_degrade(
                        bus, recorder, machine, scenario, rung=self._plan(
                            bus, scenario, LadderReason.RETRIES_EXHAUSTED
                        ).selected, reason=LadderReason.PROVIDER_ERROR,
                        attempts=attempt + 1, video_seconds=spent, poisoned=False,
                    )
                continue

            # A clip "rendered" — its seconds are charged whether or not QA passes.
            spent += scenario.target_duration_s
            self._enter(bus, machine, RenderState.QA)
            qa = scenario.qa_at(attempt)
            verdict, action, _ = decide_qa(
                qa.ccs,
                qa.style_drift,
                qa.timeline_ok,
                qa.motion,
                textual_evolution_supported=qa.textual_evolution_supported,
                retries_exhausted=self._policy.retries_exhausted(attempt),
            )
            if verdict is Verdict.PASS:
                self._poison.record_success(scenario.shot_id)
                self._enter(bus, machine, RenderState.ACCEPTED)
                return self._finish(
                    bus, recorder, machine, scenario, rung=Rung.FULL_WAN,
                    attempts=attempt + 1, video_seconds=spent, surfaced=False, poisoned=False,
                )

            self._enter(bus, machine, RenderState.REPAIR)
            decision = decide_retry(action, attempt, self._policy)
            if decision.step is RetryStep.DEGRADE:
                plan = self._plan(bus, scenario, LadderReason.RETRIES_EXHAUSTED)
                return self._finish_degrade(
                    bus, recorder, machine, scenario, rung=plan.selected,
                    reason=LadderReason.RETRIES_EXHAUSTED, attempts=attempt + 1,
                    video_seconds=spent, poisoned=False,
                )
            if decision.step is RetryStep.CONFLICT:
                outcome = self._resolve_conflict(bus, machine, scenario, attempt, spent)
                if outcome is not None:
                    return outcome  # surfaced (parked) or accepted via continuity-clear
                # honor / evolve → regenerate next attempt.
                continue
            # REGENERATE: loop to the next attempt.
            bus.publish(
                RenderEvent.retry_scheduled(
                    scenario.shot_id, attempt, action=action.value, backoff_s=decision.backoff_s
                )
            )

        # The final attempt always degrades via the cap (unreachable fallthrough).
        plan = self._plan(bus, scenario, LadderReason.RETRIES_EXHAUSTED)
        return self._finish_degrade(
            bus, recorder, machine, scenario, rung=plan.selected,
            reason=LadderReason.RETRIES_EXHAUSTED, attempts=self._policy.max_attempts,
            video_seconds=spent, poisoned=False,
        )

    def _resolve_conflict(
        self,
        bus: TelemetryBus,
        machine: ShotStateMachine,
        scenario: RenderScenario,
        attempt: int,
        spent: float,
    ) -> SimReport | None:
        """Apply the scripted §7.2 outcome; return a terminal report or ``None``."""
        machine.step(RenderState.CONFLICT)
        bus.publish(RenderEvent.state_entered(scenario.shot_id, RenderState.CONFLICT))
        action = scenario.conflict.action
        if action == "surface":
            return SimReport(
                shot_id=scenario.shot_id,
                final_state=RenderState.CONFLICT,
                rung=Rung.FULL_WAN,
                attempts=attempt + 1,
                video_seconds=spent,
                surfaced_conflict=True,
                poisoned=False,
                events=self._trace(bus),
            )
        if action == "accept":
            machine.step(RenderState.ACCEPTED)
            bus.publish(RenderEvent.state_entered(scenario.shot_id, RenderState.ACCEPTED))
            return SimReport(
                shot_id=scenario.shot_id,
                final_state=RenderState.ACCEPTED,
                rung=Rung.FULL_WAN,
                attempts=attempt + 1,
                video_seconds=spent,
                surfaced_conflict=False,
                poisoned=False,
                events=self._trace(bus),
            )
        # honor / evolve → CONFLICT -> RENDERING regen on the next loop iteration.
        return None

    # -- helpers ------------------------------------------------------------- #

    def _plan(
        self, bus: TelemetryBus, scenario: RenderScenario, reason: LadderReason
    ) -> LadderPlan:
        assets = scenario.assets
        # When degrading, the live lane is moot; surface the assets the ladder reads.
        plan = plan_ladder(
            LadderAssets(
                live_feasible=scenario.live_feasible,
                has_keyframe=assets.has_keyframe,
                has_locked_ref=assets.has_locked_ref,
                has_prev_endpoint=assets.has_prev_endpoint,
                can_image_gen=assets.can_image_gen,
                has_page_illustration=assets.has_page_illustration,
                has_narration_audio=assets.has_narration_audio,
            ),
            reason,
        )
        bus.publish(RenderEvent.rung_selected(scenario.shot_id, plan.selected, reason=reason.value))
        return plan

    def _enter(self, bus: TelemetryBus, machine: ShotStateMachine, state: RenderState) -> None:
        machine.step(state)
        bus.publish(RenderEvent.state_entered(machine.shot_id, state))

    def _finish(
        self,
        bus: TelemetryBus,
        recorder: object,
        machine: ShotStateMachine,
        scenario: RenderScenario,
        *,
        rung: Rung,
        attempts: int,
        video_seconds: float,
        surfaced: bool,
        poisoned: bool,
    ) -> SimReport:
        bus.publish(
            RenderEvent.shot_finished(
                scenario.shot_id, machine.state, rung=rung,
                video_seconds=video_seconds, attempts=attempts,
            )
        )
        return SimReport(
            shot_id=scenario.shot_id,
            final_state=machine.state,
            rung=rung,
            attempts=attempts,
            video_seconds=video_seconds,
            surfaced_conflict=surfaced,
            poisoned=poisoned,
            events=self._trace(bus),
        )

    def _finish_degrade(
        self,
        bus: TelemetryBus,
        recorder: object,
        machine: ShotStateMachine,
        scenario: RenderScenario,
        *,
        rung: Rung,
        reason: LadderReason,
        attempts: int,
        video_seconds: float,
        poisoned: bool,
    ) -> SimReport:
        # If we degrade straight from CACHE_CHECK (gate off), step into RENDERING
        # first so the machine edge CACHE_CHECK -> DEGRADED stays legal via RENDERING.
        if machine.state is RenderState.CACHE_CHECK:
            self._enter(bus, machine, RenderState.RENDERING)
        machine.step(RenderState.DEGRADED)
        bus.publish(RenderEvent.state_entered(scenario.shot_id, RenderState.DEGRADED))
        return self._finish(
            bus, recorder, machine, scenario, rung=rung, attempts=attempts,
            video_seconds=video_seconds, surfaced=False, poisoned=poisoned,
        )

    @staticmethod
    def _trace(bus: TelemetryBus) -> list[RenderEvent]:
        for sink in getattr(bus, "_sinks", []):  # the recording sink, if present
            from app.render.telemetry import RecordingSink

            if isinstance(sink, RecordingSink):
                return list(sink)
        return []


def simulate(scenario: RenderScenario, *, policy: RetryPolicy | None = None) -> SimReport:
    """Convenience: simulate one scenario with a fresh simulator + recording bus."""
    return RenderSimulator(policy=policy).run(scenario)


# --------------------------------------------------------------------------- #
# Scene-level simulation: the §9.6 DAG + per-shot control flow, fully offline
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class SceneScenario:
    """A scripted whole-scene render (the §9.6 DAG + a scenario per shot).

    ``shots`` are scene rows in reading order (``{shot_id, render_mode?, ...}``,
    the :func:`app.render.dag.build_scene_graph` shape) so the continuation chain
    is wired automatically; ``per_shot`` maps a shot id to its scripted
    :class:`RenderScenario` (a missing shot uses ``default``). This makes the
    full §9.6 scene control flow — parallel fan-out, continuation ordering, and the
    degradation distribution — provable with zero IO.
    """

    shots: list[dict[str, object]]
    per_shot: dict[str, RenderScenario] = field(default_factory=dict)
    default: RenderScenario = field(default_factory=RenderScenario)
    max_parallel: int = 4

    def scenario_for(self, shot_id: str) -> RenderScenario:
        base = self.per_shot.get(shot_id, self.default)
        # Bind the scenario's shot_id to this node so the report is per-shot.
        return RenderScenario(
            shot_id=shot_id,
            book_id=base.book_id,
            live_feasible=base.live_feasible,
            budget_low=base.budget_low,
            assets=base.assets,
            qa_sequence=list(base.qa_sequence),
            conflict=base.conflict,
            target_duration_s=base.target_duration_s,
            raise_on_attempt=base.raise_on_attempt,
            already_poisoned=base.already_poisoned,
        )


@dataclass(slots=True)
class SceneReport:
    """The outcome of simulating a whole scene."""

    reports: dict[str, SimReport] = field(default_factory=dict)
    blocked: list[str] = field(default_factory=list)
    batches: list[list[str]] = field(default_factory=list)

    @property
    def total_video_seconds(self) -> float:
        """Sum of video-seconds the scene would charge (the budget draw)."""
        return round(sum(r.video_seconds for r in self.reports.values()), 3)

    @property
    def accepted_count(self) -> int:
        return sum(1 for r in self.reports.values() if r.accepted)

    @property
    def degraded_count(self) -> int:
        return sum(1 for r in self.reports.values() if r.degraded)

    def ladder_distribution(self) -> dict[str, int]:
        """How many shots shipped at each rung (the §12.4 ladder distribution)."""
        from app.render.ladder import LADDER

        dist = {rung.value: 0 for rung in LADDER}
        for report in self.reports.values():
            dist[report.rung.value] = dist.get(report.rung.value, 0) + 1
        return dist

    @property
    def max_parallelism(self) -> int:
        """The largest simulated ready-batch (the realised fan-out)."""
        return max((len(b) for b in self.batches), default=0)


async def simulate_scene(
    scene: SceneScenario, *, policy: RetryPolicy | None = None
) -> SceneReport:
    """Simulate a whole scene's §9.6 render DAG + per-shot §9.7 control flow (zero-IO).

    Builds the dependency graph (continuation shots wait for their predecessor's
    *accepted* endpoint), runs it with the deterministic batch scheduler, and
    drives each shot through the per-shot :class:`RenderSimulator`. A continuation
    shot whose predecessor only degraded is reported ``blocked`` (no endpoint to
    extend) — exactly the engine's scene behaviour, but without ffmpeg/DB.
    """
    from app.db.models.enums import ShotStatus
    from app.render.dag import build_scene_graph, run_graph

    graph = build_scene_graph(scene.shots)
    report = SceneReport()

    async def runner(shot_id: str) -> ShotStatus:
        sim = RenderSimulator(policy=policy).run(scene.scenario_for(shot_id))
        report.reports[shot_id] = sim
        if sim.accepted:
            return ShotStatus.ACCEPTED
        if sim.surfaced_conflict:
            return ShotStatus.CONFLICT
        return ShotStatus.DEGRADED

    run = await run_graph(graph, runner, max_parallel=scene.max_parallel)
    report.blocked = list(run.blocked)
    report.batches = list(run.batches)
    return report


__all__ = [
    "ConflictOutcome",
    "QAVerdict",
    "RenderScenario",
    "RenderSimulator",
    "SceneReport",
    "SceneScenario",
    "SimReport",
    "simulate",
    "simulate_scene",
]
