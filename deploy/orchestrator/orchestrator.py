"""The deployment orchestrator state machine (kinora.md §12, §12.6).

:class:`DeploymentOrchestrator` is the engine that promotes an
:class:`~deploy.orchestrator.models.Artifact` into an
:class:`~deploy.orchestrator.models.Environment` using a rollout strategy
(blue-green or canary), gating on health + smoke + SLOs, and **automatically
rolling back on any SLO breach or health failure**. Every transition is written
to an append-only :class:`~deploy.orchestrator.audit.AuditTrail`.

Design constraints (the same that govern the §12.6 worker):

* **No cloud, no clock.** Every effect is a Protocol seam; time is injected via
  ``now``. The simulator and tests run the *entire* rollout/rollback flow with
  zero network and a virtual clock.
* **Honest safety.** Hydration refuses ``KINORA_LIVE_VIDEO`` unless explicitly
  allowed; promotion enforces the dev→staging→prod gap rule; nothing here ever
  submits a Wan task.
* **The film never hard-stops (§12.4).** A rollback always lands on the prior
  known-good digest (or 0% new traffic) — the running version keeps serving.

Flow (see :data:`~deploy.orchestrator.models.LEGAL_TRANSITIONS`)::

    PENDING → HYDRATING → PROVISIONING → ROLLING_OUT ⇄ VERIFYING → PROMOTING → SUCCEEDED
                                              │            │
                                              └────────────┴──→ ROLLING_BACK → ROLLED_BACK
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field

from deploy.orchestrator.audit import AuditSink, AuditTrail
from deploy.orchestrator.drain import DrainCoordinator, DrainResult, DrainTarget
from deploy.orchestrator.health import HealthGate
from deploy.orchestrator.hydration import HydratedConfig, Hydrator
from deploy.orchestrator.models import (
    ABORTABLE_STATES,
    TERMINAL_STATES,
    Artifact,
    DeployEvent,
    DeployState,
    Environment,
    ServiceRole,
    SLOResult,
    SLOTarget,
    can_transition,
)
from deploy.orchestrator.promotion import PromotionPipeline
from deploy.orchestrator.seams import Provisioner, TrafficRouter
from deploy.orchestrator.slo import MetricSource, SLOEvaluator
from deploy.orchestrator.smoke import SmokeGate, SmokeReport
from deploy.orchestrator.strategies import RolloutStep, Strategy


class StateTransitionError(RuntimeError):
    """Raised on an attempt to make an illegal state-machine transition."""


class RollbackError(RuntimeError):
    """Raised when the rollback itself fails (the worst case — paged, not silent)."""


@dataclass(frozen=True, slots=True)
class StepOutcome:
    """The result of one rollout step (one traffic weight)."""

    step: RolloutStep
    healthy: bool
    smoke: SmokeReport | None
    slo_results: tuple[SLOResult, ...]
    advanced: bool

    @property
    def slo_breached(self) -> bool:
        return any(r.breached for r in self.slo_results)


@dataclass(frozen=True, slots=True)
class DeploymentResult:
    """The terminal outcome of a deployment run."""

    deploy_id: str
    artifact: Artifact
    environment: Environment
    final_state: DeployState
    steps: tuple[StepOutcome, ...]
    rolled_back_to: str | None
    reason: str

    @property
    def succeeded(self) -> bool:
        return self.final_state is DeployState.SUCCEEDED

    @property
    def rolled_back(self) -> bool:
        return self.final_state is DeployState.ROLLED_BACK


@dataclass(slots=True)
class OrchestratorConfig:
    """Knobs for a single deployment run."""

    #: How many metric samples to fold per verification step before deciding.
    verify_samples: int = 5
    #: Replicas to provision for the new fleet (per role).
    replicas: int = 3
    #: Drain deadline (virtual seconds) for retiring the old render-worker.
    drain_deadline_s: float = 90.0
    #: Whether to run the smoke gate at the staging step.
    run_smoke: bool = True


@dataclass(slots=True)
class DeploymentOrchestrator:
    """Executes one rollout of ``artifact`` into ``environment``.

    All seams are injected. ``now`` is a monotonic virtual clock; the
    orchestrator never sleeps (pacing belongs to the caller/simulator), so a
    full rollout is deterministic and credit-free.
    """

    artifact: Artifact
    environment: Environment
    strategy: Strategy
    provisioner: Provisioner
    router: TrafficRouter
    health: HealthGate
    metrics: MetricSource
    slo_targets: Sequence[SLOTarget]
    hydrator: Hydrator
    promotion: PromotionPipeline
    now: Callable[[], float]
    deploy_id: str = "deploy"
    role: ServiceRole = ServiceRole.RENDER_WORKER
    smoke: SmokeGate | None = None
    drain_target: DrainTarget | None = None
    config: OrchestratorConfig = field(default_factory=OrchestratorConfig)
    audit_sink: AuditSink | None = None

    # -- internal state ----------------------------------------------------
    state: DeployState = field(default=DeployState.PENDING, init=False)
    _trail: AuditTrail = field(init=False)
    _new_slot: str | None = field(default=None, init=False)
    _prev_live: str | None = field(default=None, init=False)
    _hydrated: HydratedConfig | None = field(default=None, init=False)
    _steps: list[StepOutcome] = field(default_factory=list, init=False)
    _drain_result: DrainResult | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        self._trail = AuditTrail(self.deploy_id, sink=self.audit_sink, now=self.now)
        self._prev_live = self.promotion.live_digest(self.environment)

    # -- public surface ----------------------------------------------------

    @property
    def trail(self) -> AuditTrail:
        return self._trail

    @property
    def events(self) -> list[DeployEvent]:
        return self._trail.events()

    @property
    def drain_result(self) -> DrainResult | None:
        """The drain outcome for the retired render-worker, if a drain ran."""
        return self._drain_result

    def _transition(self, dst: DeployState, kind: str, message: str, **detail: object) -> None:
        if self.state is dst:
            return
        if not can_transition(self.state, dst):
            raise StateTransitionError(
                f"illegal transition {self.state.value} → {dst.value} ({kind})"
            )
        self.state = dst
        self._trail.record(dst, kind, message, **detail)

    async def run(self) -> DeploymentResult:
        """Execute the full rollout, auto-rolling-back on any gate failure.

        A failure raised *before* anything is provisioned (a bad promotion gate,
        an unsatisfiable config/secret hydration, a refused live-video config)
        cannot be "rolled back" — nothing is live yet — so it lands in
        :attr:`~deploy.orchestrator.models.DeployState.FAILED`. A failure *after*
        the new fleet exists routes through the safe rollback path so the
        previous known-good version keeps serving (§12.4 "never hard-stop").
        """
        try:
            return await self._run_inner()
        except StateTransitionError:
            raise
        except Exception as exc:
            self._trail.record(self.state, "error", f"unexpected: {exc}")
            if self._new_slot is None:
                # Pre-provision failure: nothing to roll back to. Fail fast.
                if can_transition(self.state, DeployState.FAILED):
                    self._transition(DeployState.FAILED, "failed", f"pre-provision: {exc}")
                return self._result(DeployState.FAILED, f"pre-provision failure: {exc}")
            return await self._rollback(reason=f"unexpected error: {exc}")

    async def _run_inner(self) -> DeploymentResult:
        plan = self.strategy.plan()
        self._trail.record(
            self.state,
            "plan",
            f"rollout {plan.strategy.value} with {len(plan)} step(s)",
            steps=[s.label for s in plan.steps],
            artifact=self.artifact.short(),
            env=self.environment.value,
        )

        # Idempotency: deploying the live digest is a no-op success (§12.1 idea).
        if self.promotion.is_idempotent(self.artifact, self.environment):
            self._transition(
                DeployState.HYDRATING, "idempotent", "digest already live; no-op"
            )
            # Walk straight to success without touching infra.
            self._fast_forward_to_success()
            return self._result(DeployState.SUCCEEDED, "already live (idempotent)")

        # 1. Promotion gate (pure check before touching anything).
        self.promotion.check_promotable(self.artifact, self.environment)

        # 2. Hydrate config/secrets.
        self._transition(DeployState.HYDRATING, "hydrate", "hydrating config + secrets")
        self._hydrated = self.hydrator.hydrate()
        self._trail.record(
            self.state,
            "hydrated",
            "config hydrated",
            fingerprint=self._hydrated.fingerprint(),
            live_video=self._hydrated.live_video_enabled,
            keys=len(self._hydrated.values),
        )

        # 3. Provision the new fleet (at 0% traffic).
        self._transition(DeployState.PROVISIONING, "provision", "provisioning new fleet")
        self._new_slot = await self.provisioner.provision(
            self.artifact, self.environment, self.role, replicas=self.config.replicas
        )
        await self.router.shift(self._new_slot, 0.0)
        self._trail.record(self.state, "provisioned", "new fleet up at 0%", slot=self._new_slot)

        # 4. Walk the rollout plan, verifying at each step.
        for index, step in enumerate(plan.steps):
            outcome = await self._run_step(index, step)
            self._steps.append(outcome)
            if not outcome.advanced:
                return await self._rollback(reason=self._step_failure_reason(outcome))

        # 5. Promote: record success, retire the old fleet with a drain.
        self._transition(DeployState.PROMOTING, "promote", "promoting new version to live")
        await self._retire_old_version()
        self.promotion.mark_succeeded(self.artifact, self.environment)
        self._transition(DeployState.SUCCEEDED, "succeeded", "deployment succeeded")
        return self._result(DeployState.SUCCEEDED, "all gates passed")

    # -- step execution ----------------------------------------------------

    async def _run_step(self, index: int, step: RolloutStep) -> StepOutcome:
        # Move the state machine: first step enters ROLLING_OUT; later steps
        # bounce VERIFYING → ROLLING_OUT for the next weight.
        if self.state is DeployState.VERIFYING:
            self._transition(
                DeployState.ROLLING_OUT, "step", f"advancing to {step.label}", weight=step.weight
            )
        else:
            self._transition(
                DeployState.ROLLING_OUT, "step", f"starting {step.label}", weight=step.weight
            )

        # Shift traffic to this step's weight.
        await self.router.shift(self._require_slot(), step.weight)
        self._trail.record(self.state, "traffic", f"weight → {step.weight:g}", label=step.label)

        # Health gate: the new fleet must be stable.
        healthy = await self.health.wait_until_stable(self._require_slot())
        self._trail.record(
            self.state,
            "health",
            "stable" if healthy else "UNHEALTHY",
            label=step.label,
            samples=self.health.window.samples,
            streak=self.health.window.streak,
        )
        if not healthy:
            return StepOutcome(step=step, healthy=False, smoke=None, slo_results=(), advanced=False)

        # Smoke gate (only at the staging / first step, when configured).
        smoke_report: SmokeReport | None = None
        if self.smoke is not None and self.config.run_smoke and (step.is_stage or index == 0):
            smoke_report = await self.smoke.run(self._require_slot())
            self._trail.record(
                self.state,
                "smoke",
                "passed" if smoke_report.passed else "FAILED",
                label=step.label,
                failures=[r.name for r in smoke_report.blocking_failures],
            )
            if not smoke_report.passed:
                return StepOutcome(
                    step=step, healthy=True, smoke=smoke_report, slo_results=(), advanced=False
                )

        # SLO verification (skipped for the blue-green staging step at 0%).
        slo_results: tuple[SLOResult, ...] = ()
        if step.verify:
            self._transition(
                DeployState.VERIFYING, "verify", f"verifying SLOs at {step.label}",
                weight=step.weight,
            )
            slo_results = await self._verify_slos(step)
            breached = [r for r in slo_results if r.breached]
            self._trail.record(
                self.state,
                "slo",
                "ok" if not breached else "BREACH",
                label=step.label,
                breaches=[r.target.name for r in breached],
                detail=[r.describe() for r in slo_results],
            )
            if breached:
                return StepOutcome(
                    step=step, healthy=True, smoke=smoke_report,
                    slo_results=slo_results, advanced=False,
                )
        else:
            # A non-verifying step (blue-green stage at 0%) still records that we
            # gated on health alone and move into VERIFYING posture for symmetry.
            self._transition(
                DeployState.VERIFYING, "verify", f"{step.label} staged (health-only)",
                weight=step.weight,
            )

        return StepOutcome(
            step=step, healthy=True, smoke=smoke_report, slo_results=slo_results, advanced=True
        )

    async def _verify_slos(self, step: RolloutStep) -> tuple[SLOResult, ...]:
        evaluator = SLOEvaluator(self.slo_targets)
        for _ in range(self.config.verify_samples):
            sample = await self.metrics.read()
            evaluator.observe(sample)
            # Early-out: once a target is in breach there's no value in sampling
            # further — fail fast and roll back (limits blast radius).
            if evaluator.breached:
                break
        return tuple(evaluator.results())

    # -- rollback ----------------------------------------------------------

    async def _rollback(self, *, reason: str) -> DeploymentResult:
        # ROLLING_OUT/VERIFYING/PROMOTING → ROLLING_BACK is legal.
        if self.state not in TERMINAL_STATES:
            self._transition(DeployState.ROLLING_BACK, "rollback", reason)
        try:
            # Flip all traffic back to the previous version (0% to the new slot).
            if self._new_slot is not None:
                await self.router.shift(self._new_slot, 0.0)
                self._trail.record(self.state, "traffic", "weight → 0 (rolled back)")
                await self.provisioner.teardown(self._new_slot)
                self._trail.record(self.state, "teardown", "new fleet retired", slot=self._new_slot)
            self.promotion.mark_rolled_back(
                self.artifact, self.environment, to=self._prev_live
            )
        except Exception as exc:
            self._trail.record(self.state, "rollback_error", str(exc))
            self._transition(DeployState.FAILED, "failed", f"rollback failed: {exc}")
            raise RollbackError(f"rollback failed: {exc}") from exc

        self._transition(
            DeployState.ROLLED_BACK, "rolled_back", "reverted to previous version",
            to=self._prev_live or "<none>",
        )
        return self._result(DeployState.ROLLED_BACK, reason)

    async def abort(self, *, reason: str) -> DeploymentResult:
        """Operator-initiated abort: route through a safe rollback.

        Legal only from an abortable (active, non-terminal) state.
        """
        if self.state in TERMINAL_STATES:
            return self._result(self.state, "already terminal")
        if self.state not in ABORTABLE_STATES:
            raise StateTransitionError(f"cannot abort from {self.state.value}")
        self._transition(DeployState.ABORTING, "abort", reason)
        # Reuse the rollback machinery; ABORTING → ROLLED_BACK is legal.
        return await self._rollback_from_abort(reason=f"aborted: {reason}")

    async def _rollback_from_abort(self, *, reason: str) -> DeploymentResult:
        try:
            if self._new_slot is not None:
                await self.router.shift(self._new_slot, 0.0)
                await self.provisioner.teardown(self._new_slot)
                self._trail.record(self.state, "teardown", "new fleet retired (abort)")
            self.promotion.mark_rolled_back(self.artifact, self.environment, to=self._prev_live)
        except Exception as exc:
            self._transition(DeployState.FAILED, "failed", f"abort rollback failed: {exc}")
            raise RollbackError(str(exc)) from exc
        self._transition(
            DeployState.ROLLED_BACK, "rolled_back", reason, to=self._prev_live or "<none>"
        )
        return self._result(DeployState.ROLLED_BACK, reason)

    # -- drain of the retiring version ------------------------------------

    async def _retire_old_version(self) -> DrainResult | None:
        """Drain the old render-worker before the new one fully takes over.

        Only render-workers hold queue leases; for other roles there is nothing
        to drain (a connection quiesce is the platform's concern). Returns the
        drain result, or ``None`` if there was no drain target / nothing to drain.
        """
        if self.drain_target is None or not self.role.drains_queue:
            return None
        coordinator = DrainCoordinator(
            self.drain_target, now=self.now, deadline_s=self.config.drain_deadline_s
        )
        result = await coordinator.drain()
        self._drain_result = result
        self._trail.record(
            self.state,
            "drain",
            f"old worker {result.phase.value}",
            phase=result.phase.value,
            inflight_start=result.inflight_at_start,
            released=result.released,
            clean=result.clean,
        )
        return result

    # -- helpers -----------------------------------------------------------

    def _require_slot(self) -> str:
        if self._new_slot is None:
            raise StateTransitionError("no provisioned slot yet")
        return self._new_slot

    def _fast_forward_to_success(self) -> None:
        for dst, kind in (
            (DeployState.PROVISIONING, "provision"),
            (DeployState.ROLLING_OUT, "step"),
            (DeployState.VERIFYING, "verify"),
            (DeployState.PROMOTING, "promote"),
            (DeployState.SUCCEEDED, "succeeded"),
        ):
            self._transition(dst, kind, "idempotent fast-forward")

    @staticmethod
    def _step_failure_reason(outcome: StepOutcome) -> str:
        if not outcome.healthy:
            return f"health gate failed at {outcome.step.label}"
        if outcome.smoke is not None and not outcome.smoke.passed:
            names = ", ".join(r.name for r in outcome.smoke.blocking_failures)
            return f"smoke gate failed at {outcome.step.label}: {names}"
        breaches = [r for r in outcome.slo_results if r.breached]
        if breaches:
            names = ", ".join(r.describe() for r in breaches)
            return f"SLO breach at {outcome.step.label}: {names}"
        return f"gate failed at {outcome.step.label}"

    def _result(self, state: DeployState, reason: str) -> DeploymentResult:
        return DeploymentResult(
            deploy_id=self.deploy_id,
            artifact=self.artifact,
            environment=self.environment,
            final_state=state,
            steps=tuple(self._steps),
            rolled_back_to=self._prev_live if state is DeployState.ROLLED_BACK else None,
            reason=reason,
        )


# A typing alias used by the simulator for run hooks.
RunHook = Callable[[DeploymentOrchestrator], Awaitable[None]]
