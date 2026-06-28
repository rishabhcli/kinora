"""In-memory fakes for every orchestrator seam (cloud-free, credit-free).

These are the doubles the tests and the :mod:`~deploy.orchestrator.simulator`
wire in place of Alibaba ESS / SLB / CloudMonitor / KMS / Tair. They are
deterministic and scriptable so a whole rollout — including a forced SLO breach
and the automatic rollback — runs offline with a virtual clock.

Nothing here imports a cloud SDK; importing this module never spends a credit.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from deploy.orchestrator.drain import DrainTarget
from deploy.orchestrator.health import HealthProbe, ProbeResult
from deploy.orchestrator.models import Artifact, Environment, HealthStatus, ServiceRole
from deploy.orchestrator.seams import Provisioner, TrafficRouter
from deploy.orchestrator.slo import MetricSource


class VirtualClock:
    """A monotonic virtual clock advanced explicitly (no real time)."""

    __slots__ = ("_t",)

    def __init__(self, start: float = 0.0) -> None:
        self._t = start

    def __call__(self) -> float:
        return self._t

    def advance(self, dt: float) -> float:
        self._t += dt
        return self._t

    @property
    def now(self) -> float:
        return self._t


@dataclass(slots=True)
class FakeProvisioner(Provisioner):
    """Records provisioned/torn-down slots; hands out deterministic slot ids."""

    fail_on: frozenset[ServiceRole] = field(default_factory=frozenset)
    provisioned: list[str] = field(default_factory=list)
    torn_down: list[str] = field(default_factory=list)
    _counter: int = field(default=0, init=False)

    async def provision(
        self, artifact: Artifact, env: Environment, role: ServiceRole, *, replicas: int
    ) -> str:
        if role in self.fail_on:
            raise RuntimeError(f"provision failed for {role.value}")
        self._counter += 1
        slot = f"{env.value}-{role.value}-{artifact.short()}-{self._counter}"
        self.provisioned.append(slot)
        return slot

    async def teardown(self, slot_id: str) -> None:
        self.torn_down.append(slot_id)


@dataclass(slots=True)
class FakeTrafficRouter(TrafficRouter):
    """Tracks the current weight per slot and the full shift history."""

    weights: dict[str, float] = field(default_factory=dict)
    history: list[tuple[str, float]] = field(default_factory=list)

    async def shift(self, new_slot: str, weight: float) -> None:
        self.weights[new_slot] = weight
        self.history.append((new_slot, weight))

    async def current_weight(self, new_slot: str) -> float:
        return self.weights.get(new_slot, 0.0)


@dataclass(slots=True)
class ScriptedHealthProbe(HealthProbe):
    """Returns a scripted sequence of :class:`ProbeResult` s.

    When the script is exhausted it repeats the last value (so a "stays healthy"
    target needs only one healthy result). ``unhealthy_after`` is a convenience:
    emit ``unhealthy_after`` healthy samples then go unhealthy forever (models a
    target that flaps then dies during the stability window).
    """

    script: list[ProbeResult] = field(default_factory=list)
    calls: int = field(default=0, init=False)

    @classmethod
    def always_healthy(cls) -> ScriptedHealthProbe:
        return cls(script=[ProbeResult.ok(postgres=True, redis=True)])

    @classmethod
    def always_unhealthy(cls, detail: str = "redis down") -> ScriptedHealthProbe:
        return cls(script=[ProbeResult.down(detail=detail, postgres=True, redis=False)])

    @classmethod
    def healthy_then_dead(cls, healthy: int) -> ScriptedHealthProbe:
        seq = [ProbeResult.ok(postgres=True, redis=True) for _ in range(healthy)]
        seq.append(ProbeResult.down(detail="crashed", postgres=True, redis=False))
        return cls(script=seq)

    async def probe(self, target: str) -> ProbeResult:
        if not self.script:
            return ProbeResult(status=HealthStatus.UNKNOWN)
        idx = min(self.calls, len(self.script) - 1)
        self.calls += 1
        return self.script[idx]


@dataclass(slots=True)
class ScriptedMetricSource(MetricSource):
    """Replays a scripted list of metric samples, repeating the last forever."""

    samples: list[Mapping[str, float]] = field(default_factory=list)
    calls: int = field(default=0, init=False)

    @classmethod
    def healthy(cls) -> ScriptedMetricSource:
        return cls(
            samples=[
                {
                    "render_success_ratio": 0.99,
                    "error_rate": 0.01,
                    "render_p99_latency_ms": 40_000.0,
                    "queue_depth_growth": -1.0,
                }
            ]
        )

    @classmethod
    def breaching(cls, metric: str, value: float) -> ScriptedMetricSource:
        base = {
            "render_success_ratio": 0.99,
            "error_rate": 0.01,
            "render_p99_latency_ms": 40_000.0,
            "queue_depth_growth": -1.0,
        }
        base[metric] = value
        return cls(samples=[base])

    async def read(self) -> Mapping[str, float]:
        if not self.samples:
            return {}
        idx = min(self.calls, len(self.samples) - 1)
        self.calls += 1
        return dict(self.samples[idx])


@dataclass(slots=True)
class FakeRenderWorker(DrainTarget):
    """A fake §12.1 render-worker for drain tests.

    Models the real ``RenderWorker``: a count of in-flight jobs that decreases
    by ``drain_rate`` each ``inflight()`` poll once cordoned (the jobs finish),
    unless ``stuck`` is set (a wedged job that never completes, forcing the
    release-at-deadline path).
    """

    inflight_jobs: int = 0
    drain_rate: int = 1
    stuck: bool = False
    cordoned: bool = field(default=False, init=False)
    terminated: bool = field(default=False, init=False)
    released_total: int = field(default=0, init=False)

    async def cordon(self) -> None:
        self.cordoned = True

    async def inflight(self) -> int:
        if self.cordoned and not self.stuck and self.inflight_jobs > 0:
            self.inflight_jobs = max(0, self.inflight_jobs - self.drain_rate)
        return self.inflight_jobs

    async def release_inflight(self) -> int:
        released = self.inflight_jobs
        self.released_total += released
        self.inflight_jobs = 0
        return released


    async def terminate(self) -> None:
        self.terminated = True


def make_artifact(
    digest_body: str = "a" * 64,
    *,
    tag: str = "v1",
    name: str = "kinora-backend",
    roles: Sequence[ServiceRole] | None = None,
    git_sha: str = "deadbeef",
) -> Artifact:
    """Convenience builder for a content-addressed test artifact."""
    return Artifact(
        name=name,
        tag=tag,
        digest=f"sha256:{digest_body}",
        roles=tuple(roles) if roles else (ServiceRole.RENDER_WORKER,),
        git_sha=git_sha,
    )


@dataclass(slots=True)
class AdvancingClockMetricSource(MetricSource):
    """A metric source that also advances a clock each read (for drain timing).

    Useful when a test wants verification sampling to consume virtual time.
    """

    inner: MetricSource
    clock: VirtualClock
    dt: float = 1.0

    async def read(self) -> Mapping[str, float]:
        self.clock.advance(self.dt)
        return await self.inner.read()


def chained_probe(probes: Iterable[Callable[[], ProbeResult]]) -> HealthProbe:
    """Build a probe from a sequence of zero-arg result factories."""
    return ScriptedHealthProbe(script=[p() for p in probes])
