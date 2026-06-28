"""Frozen value types for the deployment orchestrator (kinora.md §12, §12.6).

Everything here is a pure, immutable dataclass / enum with no I/O. The
orchestrator, strategies, SLO evaluator and simulator are all written against
these types, which is what keeps the rollout/rollback decision logic testable
with no cloud and no clock dependence (callers pass an explicit ``now`` /
monotonic time source).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from enum import StrEnum


class ServiceRole(StrEnum):
    """The §0 backend process roles, every one running the *same* image.

    Mirrors the docker-compose process model: each role is the same container
    with a different command. A deployment promotes one image digest across the
    roles it targets.
    """

    API = "api"
    INGEST_WORKER = "ingest-worker"
    RENDER_WORKER = "render-worker"
    MCP = "mcp"
    FRONTEND = "frontend"

    @property
    def drains_queue(self) -> bool:
        """Roles that pull the §12.1 render queue must drain before shutdown.

        Only the render-worker consumes the Redis priority queue. The API runs
        the Scheduler in-process but does not *claim* render jobs, so its drain
        is a connection-quiesce, not a job-drain.
        """
        return self is ServiceRole.RENDER_WORKER


class Environment(StrEnum):
    """Promotion pipeline environments, ordered dev → staging → prod."""

    DEV = "dev"
    STAGING = "staging"
    PROD = "prod"

    @property
    def rank(self) -> int:
        return {Environment.DEV: 0, Environment.STAGING: 1, Environment.PROD: 2}[self]


class RolloutStrategy(StrEnum):
    """Supported rollout strategies."""

    BLUE_GREEN = "blue-green"
    CANARY = "canary"
    RECREATE = "recreate"


class DeployState(StrEnum):
    """The deployment state machine (kinora.md §12.1 has the *render-job* state
    machine; this is the analogous machine one level up — the **deployment**).

    Legal transitions are enforced by :data:`LEGAL_TRANSITIONS`. Terminal states
    are :data:`TERMINAL_STATES`.

    ::

        PENDING ─▶ HYDRATING ─▶ PROVISIONING ─▶ ROLLING_OUT ─▶ VERIFYING
                                                     │              │
                                                     │              ├─▶ PROMOTING ─▶ SUCCEEDED
                                                     │              └─▶ ROLLING_BACK ─▶ ROLLED_BACK
                                                     └─▶ ROLLING_BACK ─▶ ROLLED_BACK
                                  (any active state) ─▶ ABORTING ─▶ ROLLED_BACK / FAILED
    """

    PENDING = "pending"
    HYDRATING = "hydrating"
    PROVISIONING = "provisioning"
    ROLLING_OUT = "rolling-out"
    VERIFYING = "verifying"
    PROMOTING = "promoting"
    SUCCEEDED = "succeeded"
    ROLLING_BACK = "rolling-back"
    ROLLED_BACK = "rolled-back"
    ABORTING = "aborting"
    FAILED = "failed"


#: Allowed transitions of the deploy state machine.
LEGAL_TRANSITIONS: Mapping[DeployState, frozenset[DeployState]] = {
    DeployState.PENDING: frozenset(
        {DeployState.HYDRATING, DeployState.ABORTING, DeployState.FAILED}
    ),
    DeployState.HYDRATING: frozenset(
        {DeployState.PROVISIONING, DeployState.ABORTING, DeployState.FAILED}
    ),
    DeployState.PROVISIONING: frozenset(
        {DeployState.ROLLING_OUT, DeployState.ABORTING, DeployState.FAILED}
    ),
    DeployState.ROLLING_OUT: frozenset(
        {DeployState.VERIFYING, DeployState.ROLLING_BACK, DeployState.ABORTING}
    ),
    DeployState.VERIFYING: frozenset(
        {
            DeployState.PROMOTING,
            DeployState.ROLLING_OUT,  # next canary step
            DeployState.ROLLING_BACK,
            DeployState.ABORTING,
        }
    ),
    DeployState.PROMOTING: frozenset(
        {DeployState.SUCCEEDED, DeployState.ROLLING_BACK, DeployState.ABORTING}
    ),
    DeployState.ROLLING_BACK: frozenset({DeployState.ROLLED_BACK, DeployState.FAILED}),
    DeployState.ABORTING: frozenset({DeployState.ROLLED_BACK, DeployState.FAILED}),
    DeployState.SUCCEEDED: frozenset(),
    DeployState.ROLLED_BACK: frozenset(),
    DeployState.FAILED: frozenset(),
}

#: States from which no further transition is possible.
TERMINAL_STATES: frozenset[DeployState] = frozenset(
    {DeployState.SUCCEEDED, DeployState.ROLLED_BACK, DeployState.FAILED}
)

#: States in which the deployment is actively touching infrastructure and can
#: therefore be aborted (which routes through ROLLING_BACK semantics).
ABORTABLE_STATES: frozenset[DeployState] = frozenset(
    {
        DeployState.PENDING,
        DeployState.HYDRATING,
        DeployState.PROVISIONING,
        DeployState.ROLLING_OUT,
        DeployState.VERIFYING,
        DeployState.PROMOTING,
    }
)


def can_transition(src: DeployState, dst: DeployState) -> bool:
    """True iff ``src ─▶ dst`` is a legal state-machine edge."""
    return dst in LEGAL_TRANSITIONS.get(src, frozenset())


class HealthStatus(StrEnum):
    """Outcome of a single health probe."""

    HEALTHY = "healthy"
    UNHEALTHY = "unhealthy"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class Artifact:
    """An immutable build artifact (a container image) being promoted.

    Identified by its content digest. ``digest`` is the source of truth for
    idempotency — re-deploying the same digest to a target that already runs it
    is a no-op (mirrors the §12.1 ``shot_hash`` idempotency principle, one level
    up at the deployment).
    """

    name: str
    tag: str
    digest: str
    roles: tuple[ServiceRole, ...] = field(
        default=(
            ServiceRole.API,
            ServiceRole.INGEST_WORKER,
            ServiceRole.RENDER_WORKER,
            ServiceRole.MCP,
        )
    )
    built_at: float = 0.0
    git_sha: str = ""
    metadata: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.digest:
            raise ValueError("Artifact.digest is required (content addressing)")
        if not self.name:
            raise ValueError("Artifact.name is required")
        if not self.roles:
            raise ValueError("Artifact.roles must be non-empty")

    @property
    def ref(self) -> str:
        """A human-readable image ref, e.g. ``kinora-backend:abc123@sha256:…``."""
        return f"{self.name}:{self.tag}@{self.digest}"

    def short(self) -> str:
        """A short digest for logs/events (first 12 hex chars after ``sha256:``)."""
        body = self.digest.split(":", 1)[-1]
        return body[:12]


@dataclass(frozen=True, slots=True)
class SLOTarget:
    """A service-level objective threshold the rollout must hold during
    verification (kinora.md §12.5 lists the per-shot/per-session signals; these
    are the rollout-gating subset).

    Each target is a named metric with a bound and a direction. A breach of any
    target during the stability window trips an automatic rollback.
    """

    name: str
    threshold: float
    #: ``True`` if higher is better (e.g. success ratio); the metric breaches
    #: when ``value < threshold``. ``False`` if lower is better (e.g. p99
    #: latency, error rate); breaches when ``value > threshold``.
    higher_is_better: bool = True
    #: Consecutive breaching samples tolerated before the target is "breached".
    #: 1 = breach on the first bad sample; 3 = require three in a row.
    breach_tolerance: int = 1
    unit: str = ""

    def __post_init__(self) -> None:
        if self.breach_tolerance < 1:
            raise ValueError("breach_tolerance must be >= 1")

    def is_breaching(self, value: float) -> bool:
        """True iff a *single* sample value violates the target."""
        if self.higher_is_better:
            return value < self.threshold
        return value > self.threshold


@dataclass(frozen=True, slots=True)
class SLOResult:
    """Result of evaluating one SLO target over a window of samples."""

    target: SLOTarget
    breached: bool
    worst_value: float
    samples: int
    consecutive_breaches: int

    def describe(self) -> str:
        verb = "BREACH" if self.breached else "ok"
        return (
            f"{self.target.name}={self.worst_value:g}{self.target.unit} "
            f"(thr {self.target.threshold:g}, {verb}, "
            f"{self.consecutive_breaches}/{self.target.breach_tolerance})"
        )


@dataclass(frozen=True, slots=True)
class DeployEvent:
    """One append-only audit record in a deployment's history."""

    seq: int
    at: float
    deploy_id: str
    state: DeployState
    kind: str
    message: str
    detail: Mapping[str, object] = field(default_factory=dict)

    def with_seq(self, seq: int) -> DeployEvent:
        return replace(self, seq=seq)
