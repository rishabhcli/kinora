"""A deterministic, cloud-free deployment simulator (kinora.md §12.6).

This is the *proof* that the rollout/rollback logic is correct without any
Alibaba/cloud call. It wires :class:`~deploy.orchestrator.orchestrator.DeploymentOrchestrator`
to the in-memory :mod:`~deploy.orchestrator.fakes` and lets you script a world:

* the health trajectory of the new fleet (healthy / flaps / dies),
* the SLO metric trajectory (holds / breaches a named metric),
* the smoke result,
* the in-flight job count of the retiring render-worker.

It returns a :class:`SimReport` carrying the terminal state, the full audit
trail, the traffic-shift history, and the drain outcome — everything a §12.6
recording would show, but offline and free.

Run it as a script::

    python -m deploy.orchestrator.simulator            # the happy canary path
    python -m deploy.orchestrator.simulator --scenario slo-breach
    python -m deploy.orchestrator.simulator --scenario all
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from dataclasses import dataclass, field

from deploy.orchestrator.audit import AuditTrail
from deploy.orchestrator.drain import DrainResult
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
    SLOTarget,
)
from deploy.orchestrator.orchestrator import (
    DeploymentOrchestrator,
    DeploymentResult,
    OrchestratorConfig,
)
from deploy.orchestrator.promotion import PromotionPipeline
from deploy.orchestrator.slo import DEFAULT_RENDER_SLOS
from deploy.orchestrator.smoke import ScriptedSmokeCheck, SmokeCheck, SmokeGate, SmokeOutcome
from deploy.orchestrator.strategies import (
    Strategy,
    strategy_for,
)


def _good_config() -> dict[str, str]:
    """A complete, non-spending render-worker config (KINORA_LIVE_VIDEO off)."""
    return {
        "OSS_ENDPOINT": "https://oss-ap-southeast-1.aliyuncs.com",
        "OSS_BUCKET": "kinora-assets",
        "KINORA_LIVE_VIDEO": "false",
        "VIDEO_MODEL": "wan2.1-t2v-turbo",
    }


def _good_secrets() -> dict[str, str]:
    return {
        "DASHSCOPE_API_KEY": "sk-test-not-real",
        "OSS_AK": "ak-test",
        "OSS_SECRET": "secret-test",
        "REDIS_URL": "redis://:pw@tair-host:6379/0",
        "DATABASE_URL": "postgresql+asyncpg://kinora:pw@rds-host:5432/kinora",
    }


@dataclass(slots=True)
class SimScenario:
    """A scripted world for one simulated deployment."""

    name: str
    strategy: RolloutStrategy = RolloutStrategy.CANARY
    environment: Environment = Environment.STAGING
    health: ScriptedHealthProbe = field(default_factory=ScriptedHealthProbe.always_healthy)
    metrics: ScriptedMetricSource = field(default_factory=ScriptedMetricSource.healthy)
    smoke_outcome: SmokeOutcome = field(default_factory=lambda: SmokeOutcome.ok("scripted"))
    inflight_jobs: int = 4
    stuck_worker: bool = False
    config: dict[str, str] = field(default_factory=_good_config)
    secrets: dict[str, str] = field(default_factory=_good_secrets)
    slo_targets: Sequence[SLOTarget] = DEFAULT_RENDER_SLOS
    artifact: Artifact | None = None
    prior_live_digest: str | None = None
    allow_live_video: bool = False
    stability_required: int = 2


@dataclass(slots=True)
class SimReport:
    """The full record of a simulated deployment."""

    scenario: str
    result: DeploymentResult
    trail: AuditTrail
    router_history: list[tuple[str, float]]
    provisioned: list[str]
    torn_down: list[str]
    drain: DrainResult | None
    final_clock: float

    @property
    def final_state(self) -> DeployState:
        return self.result.final_state

    @property
    def succeeded(self) -> bool:
        return self.result.succeeded

    @property
    def rolled_back(self) -> bool:
        return self.result.rolled_back

    @property
    def max_weight_reached(self) -> float:
        return max((w for _, w in self.router_history), default=0.0)

    def transcript(self) -> str:
        return self.trail.render()


async def simulate(scenario: SimScenario) -> SimReport:
    """Run one scripted deployment end-to-end with no cloud and a virtual clock."""
    clock = VirtualClock()
    artifact = scenario.artifact or make_artifact(roles=(ServiceRole.RENDER_WORKER,))

    provisioner = FakeProvisioner()
    router = FakeTrafficRouter()
    health = HealthGate(
        scenario.health,
        window=StabilityWindow(required=scenario.stability_required, max_samples=12),
    )
    hydrator = Hydrator(
        config_source=DictConfigSource(scenario.config),
        secret_source=DictSecretSource(scenario.secrets),
        allow_live_video=scenario.allow_live_video,
    )
    promotion = PromotionPipeline(now=clock)
    # Seed lower-environment success so a staging/prod promotion passes the gap
    # rule, and any prior-live digest so rollback has a known-good target.
    if scenario.environment is not Environment.DEV:
        lower = (
            Environment.DEV
            if scenario.environment is Environment.STAGING
            else Environment.STAGING
        )
        promotion.mark_succeeded(artifact, lower)
    if scenario.prior_live_digest:
        prior = make_artifact(digest_body=scenario.prior_live_digest.ljust(64, "0")[:64])
        promotion.mark_succeeded(prior, scenario.environment)

    smoke_check = SmokeCheck(name="scripted", run=ScriptedSmokeCheck(scenario.smoke_outcome))
    smoke = SmokeGate(checks=[smoke_check])

    worker = FakeRenderWorker(inflight_jobs=scenario.inflight_jobs, stuck=scenario.stuck_worker)

    strategy: Strategy = strategy_for(scenario.strategy)

    orch = DeploymentOrchestrator(
        artifact=artifact,
        environment=scenario.environment,
        strategy=strategy,
        provisioner=provisioner,
        router=router,
        health=health,
        metrics=scenario.metrics,
        slo_targets=scenario.slo_targets,
        hydrator=hydrator,
        promotion=promotion,
        now=clock,
        deploy_id=f"sim-{scenario.name}",
        role=ServiceRole.RENDER_WORKER,
        smoke=smoke,
        drain_target=worker,
        config=OrchestratorConfig(verify_samples=4, replicas=3, drain_deadline_s=10.0),
    )

    result = await orch.run()

    return SimReport(
        scenario=scenario.name,
        result=result,
        trail=orch.trail,
        router_history=list(router.history),
        provisioned=list(provisioner.provisioned),
        torn_down=list(provisioner.torn_down),
        # The orchestrator exposes the real DrainResult it computed (None when
        # the role doesn't drain the queue, or rollback happened before promote).
        drain=orch.drain_result,
        final_clock=clock.now,
    )


# ---------------------------------------------------------------------------
# Canonical scenarios
# ---------------------------------------------------------------------------


def scenario_happy_canary() -> SimScenario:
    return SimScenario(name="happy-canary", strategy=RolloutStrategy.CANARY)


def scenario_happy_blue_green() -> SimScenario:
    return SimScenario(name="happy-blue-green", strategy=RolloutStrategy.BLUE_GREEN)


def scenario_slo_breach() -> SimScenario:
    """The new version raises the error rate above its SLO → auto rollback."""
    return SimScenario(
        name="slo-breach",
        strategy=RolloutStrategy.CANARY,
        metrics=ScriptedMetricSource.breaching("error_rate", 0.40),
        prior_live_digest="b" * 12,
    )


def scenario_health_fail() -> SimScenario:
    """The green fleet never stabilises → rollback before any traffic shift."""
    return SimScenario(
        name="health-fail",
        strategy=RolloutStrategy.BLUE_GREEN,
        health=ScriptedHealthProbe.always_unhealthy(),
        prior_live_digest="c" * 12,
    )


def scenario_smoke_fail() -> SimScenario:
    """Smoke gate fails at staging → rollback before SLO verification."""
    return SimScenario(
        name="smoke-fail",
        strategy=RolloutStrategy.CANARY,
        smoke_outcome=SmokeOutcome.fail("degraded-render did not produce Ken-Burns mp4"),
        prior_live_digest="d" * 12,
    )


def scenario_stuck_drain() -> SimScenario:
    """The retiring worker has a wedged job → released at the drain deadline."""
    return SimScenario(
        name="stuck-drain",
        strategy=RolloutStrategy.BLUE_GREEN,
        inflight_jobs=3,
        stuck_worker=True,
    )


def scenario_live_video_blocked() -> SimScenario:
    """Config turns KINORA_LIVE_VIDEO on without permission → hydration refuses."""
    cfg = _good_config()
    cfg["KINORA_LIVE_VIDEO"] = "true"
    return SimScenario(name="live-video-blocked", config=cfg, allow_live_video=False)


ALL_SCENARIOS = {
    "happy-canary": scenario_happy_canary,
    "happy-blue-green": scenario_happy_blue_green,
    "slo-breach": scenario_slo_breach,
    "health-fail": scenario_health_fail,
    "smoke-fail": scenario_smoke_fail,
    "stuck-drain": scenario_stuck_drain,
    "live-video-blocked": scenario_live_video_blocked,
}


async def _run_named(name: str) -> SimReport:
    factory = ALL_SCENARIOS[name]
    return await simulate(factory())


def _format_report(report: SimReport) -> str:
    lines = [
        f"=== scenario: {report.scenario} ===",
        f"final state : {report.final_state.value}",
        f"reason      : {report.result.reason}",
        f"max weight  : {report.max_weight_reached:g}",
        f"provisioned : {report.provisioned}",
        f"torn down   : {report.torn_down}",
        f"rolled back : {report.result.rolled_back_to or '-'}",
        f"clock       : {report.final_clock:g}",
        "--- audit trail ---",
        report.transcript(),
    ]
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    import asyncio

    parser = argparse.ArgumentParser(description="Kinora deployment simulator (§12.6)")
    parser.add_argument(
        "--scenario",
        default="happy-canary",
        choices=[*ALL_SCENARIOS.keys(), "all"],
        help="which scenario to simulate",
    )
    args = parser.parse_args(argv)

    names = list(ALL_SCENARIOS) if args.scenario == "all" else [args.scenario]
    for name in names:
        report = asyncio.run(_run_named(name))
        print(_format_report(report))
        print()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
