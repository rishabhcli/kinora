"""End-to-end orchestrator tests: rollout, SLO-gated auto-rollback, drain, abort.

Every test runs fully offline (in-memory fakes, virtual clock) — the proof that
the rollout/rollback decision logic is correct with zero cloud and zero credits.
"""

from __future__ import annotations

import pytest

from deploy.orchestrator.fakes import (
    FakeProvisioner,
    FakeRenderWorker,
    FakeTrafficRouter,
    ScriptedHealthProbe,
    ScriptedMetricSource,
    VirtualClock,
    make_artifact,
)
from deploy.orchestrator.health import HealthGate, StabilityWindow
from deploy.orchestrator.hydration import DictConfigSource, DictSecretSource, Hydrator
from deploy.orchestrator.models import (
    Artifact,
    DeployState,
    Environment,
    RolloutStrategy,
    ServiceRole,
)
from deploy.orchestrator.orchestrator import (
    DeploymentOrchestrator,
    OrchestratorConfig,
    RollbackError,
    StateTransitionError,
)
from deploy.orchestrator.promotion import PromotionPipeline
from deploy.orchestrator.seams import Provisioner
from deploy.orchestrator.slo import DEFAULT_RENDER_SLOS
from deploy.orchestrator.smoke import ScriptedSmokeCheck, SmokeCheck, SmokeGate, SmokeOutcome
from deploy.orchestrator.strategies import CanaryStrategy, strategy_for


def _config() -> dict[str, str]:
    return {
        "OSS_ENDPOINT": "https://oss-ap-southeast-1.aliyuncs.com",
        "OSS_BUCKET": "kinora-assets",
        "KINORA_LIVE_VIDEO": "false",
    }


def _secrets() -> dict[str, str]:
    return {
        "DASHSCOPE_API_KEY": "sk-test",
        "OSS_AK": "ak",
        "OSS_SECRET": "shh",
        "REDIS_URL": "redis://:pw@host:6379/0",
        "DATABASE_URL": "postgresql+asyncpg://u:pw@host:5432/db",
    }


def _build(
    *,
    strategy: RolloutStrategy = RolloutStrategy.CANARY,
    health: ScriptedHealthProbe | None = None,
    metrics: ScriptedMetricSource | None = None,
    smoke_pass: bool = True,
    env: Environment = Environment.STAGING,
    prior_live: str | None = None,
    provisioner: Provisioner | None = None,
    drain_target: FakeRenderWorker | None = None,
    clock: VirtualClock | None = None,
    role: ServiceRole = ServiceRole.RENDER_WORKER,
) -> DeploymentOrchestrator:
    clock = clock or VirtualClock()
    artifact = make_artifact(roles=(role,))
    promotion = PromotionPipeline(now=clock)
    # Seed lower env so the gap rule passes; seed prior-live for rollback target.
    if env is not Environment.DEV:
        lower = Environment.DEV if env is Environment.STAGING else Environment.STAGING
        promotion.mark_succeeded(artifact, lower)
    if prior_live:
        promotion.mark_succeeded(make_artifact(digest_body=prior_live.ljust(64, "0")[:64]), env)

    smoke = SmokeGate(
        checks=[SmokeCheck("scripted", ScriptedSmokeCheck(SmokeOutcome(passed=smoke_pass)))]
    )
    return DeploymentOrchestrator(
        artifact=artifact,
        environment=env,
        strategy=strategy_for(strategy),
        provisioner=provisioner or FakeProvisioner(),
        router=FakeTrafficRouter(),
        health=HealthGate(
            health or ScriptedHealthProbe.always_healthy(),
            window=StabilityWindow(required=2, max_samples=8),
        ),
        metrics=metrics or ScriptedMetricSource.healthy(),
        slo_targets=DEFAULT_RENDER_SLOS,
        hydrator=Hydrator(
            config_source=DictConfigSource(_config()),
            secret_source=DictSecretSource(_secrets()),
        ),
        promotion=promotion,
        now=clock,
        role=role,
        smoke=smoke,
        drain_target=drain_target or FakeRenderWorker(inflight_jobs=2),
        config=OrchestratorConfig(verify_samples=3, replicas=2, drain_deadline_s=10.0),
    )


# -- happy paths ----------------------------------------------------------


async def test_canary_succeeds_and_walks_all_weights() -> None:
    orch = _build(strategy=RolloutStrategy.CANARY)
    result = await orch.run()
    assert result.succeeded
    assert orch.state is DeployState.SUCCEEDED
    weights = [w for _, w in orch.router.history]  # type: ignore[attr-defined]
    # 0% provision, then 5/25/50/100.
    assert weights == [0.0, 0.05, 0.25, 0.5, 1.0]
    assert len(result.steps) == 4
    assert all(s.advanced for s in result.steps)


async def test_blue_green_stages_then_cuts_over() -> None:
    orch = _build(strategy=RolloutStrategy.BLUE_GREEN)
    result = await orch.run()
    assert result.succeeded
    weights = [w for _, w in orch.router.history]  # type: ignore[attr-defined]
    assert weights == [0.0, 0.0, 1.0]  # provision-0, stage-0, cutover-1


async def test_success_marks_promotion_live() -> None:
    clock = VirtualClock()
    orch = _build(strategy=RolloutStrategy.CANARY, clock=clock)
    await orch.run()
    assert orch.promotion.live_digest(Environment.STAGING) == orch.artifact.digest


async def test_success_drains_the_old_render_worker() -> None:
    worker = FakeRenderWorker(inflight_jobs=3)
    orch = _build(drain_target=worker)
    await orch.run()
    assert worker.cordoned and worker.terminated
    drain_events = orch.trail.by_kind("drain")
    assert drain_events and drain_events[-1].detail["clean"] is True


async def test_non_render_role_does_not_drain() -> None:
    worker = FakeRenderWorker(inflight_jobs=3)
    orch = _build(role=ServiceRole.API, drain_target=worker)
    result = await orch.run()
    assert result.succeeded
    # API role has nothing to drain.
    assert worker.cordoned is False
    assert orch.trail.by_kind("drain") == []


# -- auto-rollback paths --------------------------------------------------


async def test_slo_breach_rolls_back_at_first_canary_step() -> None:
    orch = _build(
        strategy=RolloutStrategy.CANARY,
        metrics=ScriptedMetricSource.breaching("error_rate", 0.5),
        prior_live="b" * 12,
    )
    result = await orch.run()
    assert result.rolled_back
    assert orch.state is DeployState.ROLLED_BACK
    # Only reached the 5% step before rolling back.
    weights = [w for _, w in orch.router.history]  # type: ignore[attr-defined]
    assert max(weights) == 0.05
    # Ended back at 0% to the new slot.
    assert weights[-1] == 0.0
    assert result.rolled_back_to is not None


async def test_health_failure_rolls_back_before_traffic() -> None:
    orch = _build(
        strategy=RolloutStrategy.CANARY,
        health=ScriptedHealthProbe.always_unhealthy(),
        prior_live="c" * 12,
    )
    result = await orch.run()
    assert result.rolled_back
    assert "health gate failed" in result.reason


async def test_smoke_failure_rolls_back_before_slo() -> None:
    orch = _build(strategy=RolloutStrategy.CANARY, smoke_pass=False, prior_live="d" * 12)
    result = await orch.run()
    assert result.rolled_back
    assert "smoke gate failed" in result.reason
    # SLO verification never ran.
    assert orch.trail.by_kind("slo") == []


async def test_rolled_back_artifact_is_not_marked_succeeded() -> None:
    orch = _build(
        metrics=ScriptedMetricSource.breaching("error_rate", 0.5), prior_live="b" * 12
    )
    await orch.run()
    assert not orch.promotion.has_succeeded(Environment.STAGING, orch.artifact.digest)


def _sample(success: float, error: float) -> dict[str, float]:
    return {
        "render_success_ratio": success,
        "error_rate": error,
        "render_p99_latency_ms": 10.0,
        "queue_depth_growth": -1.0,
    }


async def test_late_canary_breach_limits_blast_radius() -> None:
    # Healthy through 5%/25% (3 verify samples each), breach at the 50% step.
    ok = _sample(0.99, 0.01)
    bad = _sample(0.10, 0.9)
    metrics = ScriptedMetricSource(samples=[ok, ok, ok, ok, ok, ok, bad])
    orch = _build(strategy=RolloutStrategy.CANARY, metrics=metrics, prior_live="e" * 12)
    result = await orch.run()
    assert result.rolled_back
    weights = [w for _, w in orch.router.history]  # type: ignore[attr-defined]
    # Reached 50% (the breaching step) but never 100%.
    assert 0.5 in weights
    assert 1.0 not in weights


# -- failures before provisioning ----------------------------------------


async def test_unsatisfiable_promotion_gate_does_not_touch_infra() -> None:
    clock = VirtualClock()
    # Build a prod deploy without seeding staging success → gap rule fails.
    artifact = make_artifact(roles=(ServiceRole.RENDER_WORKER,))
    promotion = PromotionPipeline(now=clock)
    provisioner = FakeProvisioner()
    orch = DeploymentOrchestrator(
        artifact=artifact,
        environment=Environment.PROD,
        strategy=CanaryStrategy(),
        provisioner=provisioner,
        router=FakeTrafficRouter(),
        health=HealthGate(ScriptedHealthProbe.always_healthy()),
        metrics=ScriptedMetricSource.healthy(),
        slo_targets=DEFAULT_RENDER_SLOS,
        hydrator=Hydrator(
            config_source=DictConfigSource(_config()),
            secret_source=DictSecretSource(_secrets()),
        ),
        promotion=promotion,
        now=clock,
    )
    result = await orch.run()
    assert result.final_state is DeployState.FAILED
    assert provisioner.provisioned == []  # nothing was provisioned


async def test_live_video_config_refused_before_provisioning() -> None:
    clock = VirtualClock()
    cfg = _config()
    cfg["KINORA_LIVE_VIDEO"] = "true"
    artifact = make_artifact(roles=(ServiceRole.RENDER_WORKER,))
    promotion = PromotionPipeline(now=clock)
    promotion.mark_succeeded(artifact, Environment.DEV)
    provisioner = FakeProvisioner()
    orch = DeploymentOrchestrator(
        artifact=artifact,
        environment=Environment.STAGING,
        strategy=CanaryStrategy(),
        provisioner=provisioner,
        router=FakeTrafficRouter(),
        health=HealthGate(ScriptedHealthProbe.always_healthy()),
        metrics=ScriptedMetricSource.healthy(),
        slo_targets=DEFAULT_RENDER_SLOS,
        hydrator=Hydrator(
            config_source=DictConfigSource(cfg),
            secret_source=DictSecretSource(_secrets()),
            allow_live_video=False,
        ),
        promotion=promotion,
        now=clock,
    )
    result = await orch.run()
    assert result.final_state is DeployState.FAILED
    assert "KINORA_LIVE_VIDEO" in result.reason
    assert provisioner.provisioned == []


# -- idempotency ----------------------------------------------------------


async def test_deploying_live_digest_is_idempotent_noop() -> None:
    clock = VirtualClock()
    orch = _build(clock=clock)
    # Pre-mark the artifact as already live in staging.
    orch.promotion.mark_succeeded(orch.artifact, Environment.STAGING)
    provisioner_calls_before = list(orch.provisioner.provisioned)  # type: ignore[attr-defined]
    result = await orch.run()
    assert result.succeeded
    assert "idempotent" in result.reason
    # No new provisioning happened.
    assert orch.provisioner.provisioned == provisioner_calls_before  # type: ignore[attr-defined]


# -- abort ----------------------------------------------------------------


async def test_abort_from_terminal_state_is_noop() -> None:
    orch = _build()
    await orch.run()
    assert orch.state is DeployState.SUCCEEDED
    result = await orch.abort(reason="too late")
    assert result.final_state is DeployState.SUCCEEDED


async def test_abort_mid_flight_rolls_back_safely() -> None:
    # Drive the orchestrator into a mid-flight abortable state (a provisioned
    # green fleet at some traffic), then an operator aborts.
    orch = _build(prior_live="f" * 12)
    orch._transition(DeployState.HYDRATING, "hydrate", "h")  # noqa: SLF001
    orch._transition(DeployState.PROVISIONING, "provision", "p")  # noqa: SLF001
    orch._new_slot = await orch.provisioner.provision(  # noqa: SLF001
        orch.artifact, orch.environment, orch.role, replicas=2
    )
    await orch.router.shift(orch._new_slot, 0.25)  # noqa: SLF001
    orch._transition(DeployState.ROLLING_OUT, "step", "s")  # noqa: SLF001

    result = await orch.abort(reason="operator pulled the cord")

    assert result.final_state is DeployState.ROLLED_BACK
    assert "aborted" in result.reason
    # The green fleet was torn down and traffic returned to 0.
    assert orch._new_slot in orch.provisioner.torn_down  # type: ignore[attr-defined]  # noqa: SLF001
    assert orch.router.weights[orch._new_slot] == 0.0  # type: ignore[attr-defined]  # noqa: SLF001


async def test_abort_from_non_abortable_state_raises() -> None:
    orch = _build()
    orch._transition(DeployState.HYDRATING, "h", "h")  # noqa: SLF001
    orch._transition(DeployState.PROVISIONING, "p", "p")  # noqa: SLF001
    orch._transition(DeployState.ROLLING_OUT, "s", "s")  # noqa: SLF001
    orch._transition(DeployState.ROLLING_BACK, "rb", "rb")  # noqa: SLF001
    # ROLLING_BACK is not abortable.
    with pytest.raises(StateTransitionError):
        await orch.abort(reason="nope")


async def test_rollback_failure_surfaces_rollback_error() -> None:
    # A router that fails the rollback shift turns a rollback into a hard FAILED
    # + RollbackError (the worst case — surfaced loudly, never swallowed).
    class _FailingRouter(FakeTrafficRouter):
        async def shift(self, new_slot: str, weight: float) -> None:
            if weight == 0.0 and new_slot in self.weights:
                raise RuntimeError("SLB listener update failed")
            await super().shift(new_slot, weight)

    orch = _build(
        metrics=ScriptedMetricSource.breaching("error_rate", 0.5),
        prior_live="b" * 12,
    )
    orch.router = _FailingRouter()
    with pytest.raises(RollbackError):
        await orch.run()
    assert orch.state is DeployState.FAILED


# -- failure during provisioning rolls back -------------------------------


class _FailingProvisioner(FakeProvisioner):
    async def provision(
        self,
        artifact: Artifact,
        env: Environment,
        role: ServiceRole,
        *,
        replicas: int,
    ) -> str:
        raise RuntimeError("ESS scaling group quota exceeded")


async def test_provision_failure_before_slot_fails_cleanly() -> None:
    orch = _build(provisioner=_FailingProvisioner())
    result = await orch.run()
    # provision() raises before _new_slot is set → FAILED (nothing to roll back).
    assert result.final_state is DeployState.FAILED
    assert "ESS scaling group quota" in result.reason


# -- state machine guard --------------------------------------------------


async def test_illegal_transition_raises() -> None:
    orch = _build()
    await orch.run()
    # Manually attempting an illegal transition surfaces a StateTransitionError.
    with pytest.raises(StateTransitionError):
        orch._transition(DeployState.ROLLING_OUT, "bad", "illegal")  # noqa: SLF001
