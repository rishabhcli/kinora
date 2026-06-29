"""Model value objects: kinds, stages, performance profiles, and versions.

These are the immutable records the registry stores and the simulator reads. A
:class:`ModelVersion` couples *identity* (name + semver-ish version + lineage)
with a *serving profile* (the latency/throughput characteristics the discrete-event
simulator needs to model it on the simulated GPU) and a *promotion stage*.

Versions use the ``app.llmops.semver`` parser so model versions order the same way
prompt versions do, without re-implementing semver. The dependency is one-way
(this facet → the existing pure semver helper) and additive — it does not touch
``llmops``.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import StrEnum

from app.llmops.semver import SemVer
from app.mlplatform.serving.errors import RegistryError


class ModelKind(StrEnum):
    """The role a model plays in Kinora's stack.

    Maps onto the model classes the design discusses (§11 model stack): the
    *reasoning* brain, the *judge* that scores generations, the learned *reward*
    model, and the *draft* model used as the speculative-decoding proposer.
    """

    REASONING = "reasoning"
    JUDGE = "judge"
    REWARD = "reward"
    DRAFT = "draft"
    EMBEDDING = "embedding"


class Stage(StrEnum):
    """The staged-promotion ladder a model version climbs.

    Promotion is strictly one rung at a time up the ladder; rollback moves down.
    ``ARCHIVED`` is terminal (a retired version), reachable from any stage.
    """

    DEV = "dev"
    STAGING = "staging"
    CANARY = "canary"
    PROD = "prod"
    ARCHIVED = "archived"


#: The ordered promotion ladder (excludes the terminal ARCHIVED sink).
PROMOTION_LADDER: tuple[Stage, ...] = (Stage.DEV, Stage.STAGING, Stage.CANARY, Stage.PROD)


def next_stage(stage: Stage) -> Stage | None:
    """Return the stage one rung above ``stage`` on the ladder, or ``None`` at top."""
    if stage not in PROMOTION_LADDER:
        return None
    idx = PROMOTION_LADDER.index(stage)
    if idx + 1 >= len(PROMOTION_LADDER):
        return None
    return PROMOTION_LADDER[idx + 1]


def prev_stage(stage: Stage) -> Stage | None:
    """Return the stage one rung below ``stage`` on the ladder, or ``None`` at bottom."""
    if stage not in PROMOTION_LADDER:
        return None
    idx = PROMOTION_LADDER.index(stage)
    if idx == 0:
        return None
    return PROMOTION_LADDER[idx - 1]


@dataclass(frozen=True, slots=True)
class ModelProfile:
    """The serving characteristics the simulator needs to model a model.

    All times are in **simulated milliseconds**. The two costs that matter for an
    autoregressive transformer served with continuous batching:

    * ``prefill_ms_per_token`` — the per-token cost of the one-shot prompt pass.
    * ``decode_ms_per_token`` — the per-token cost of one decode step for *one*
      sequence in the batch. A batch of ``B`` sequences pays roughly the *max* over
      the batch plus a small per-extra-sequence overhead, because decode is
      memory-bandwidth-bound and the batch shares the weight read.

    ``kv_bytes_per_token`` sizes a sequence's KV-cache footprint, which the paged
    cache turns into a block count. ``params_billions`` and ``cost_per_1k_tokens``
    feed the cost model. ``accept_rate`` is only meaningful for a DRAFT model: the
    probability the target accepts one of its proposed tokens (speculative decode).
    """

    decode_ms_per_token: float
    prefill_ms_per_token: float
    kv_bytes_per_token: int
    params_billions: float
    cost_per_1k_tokens: float
    batch_overhead_ms_per_seq: float = 0.05
    accept_rate: float = 0.0
    max_context_tokens: int = 8192

    def __post_init__(self) -> None:
        if self.decode_ms_per_token <= 0 or self.prefill_ms_per_token <= 0:
            raise RegistryError("per-token times must be positive")
        if self.kv_bytes_per_token <= 0:
            raise RegistryError("kv_bytes_per_token must be positive")
        if self.params_billions <= 0:
            raise RegistryError("params_billions must be positive")
        if self.cost_per_1k_tokens < 0:
            raise RegistryError("cost_per_1k_tokens must be non-negative")
        if not 0.0 <= self.accept_rate <= 1.0:
            raise RegistryError("accept_rate must be in [0, 1]")
        if self.max_context_tokens <= 0:
            raise RegistryError("max_context_tokens must be positive")


@dataclass(frozen=True, slots=True)
class ModelVersion:
    """An immutable registered model version.

    Identity is ``(name, version)``. ``parent`` and ``teacher`` capture lineage:
    ``parent`` is the previous version this one descends from (a fine-tune or a
    config bump); ``teacher`` is the model it was *distilled* from (set by the
    distillation pipeline). Both are ``"name@version"`` strings the registry
    resolves and validates.

    ``stage`` is the current promotion rung. ``gate_passed`` records whether this
    version has cleared its eval gate (a precondition for leaving ``DEV``).
    """

    name: str
    version: str
    kind: ModelKind
    profile: ModelProfile
    stage: Stage = Stage.DEV
    parent: str | None = None
    teacher: str | None = None
    gate_passed: bool = False
    tags: tuple[str, ...] = ()
    metadata: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Validate the version is a parseable semver (raises InvalidVersionError).
        SemVer.parse(self.version)
        if not self.name:
            raise RegistryError("model name must be non-empty")

    @property
    def ref(self) -> str:
        """The canonical ``name@version`` reference for this version."""
        return f"{self.name}@{self.version}"

    @property
    def semver(self) -> SemVer:
        """The parsed semantic version (for ordering)."""
        return SemVer.parse(self.version)

    def with_stage(self, stage: Stage) -> ModelVersion:
        """Return a copy promoted/demoted to ``stage`` (registry rows are immutable)."""
        return replace(self, stage=stage)

    def with_gate(self, *, passed: bool) -> ModelVersion:
        """Return a copy recording the eval-gate outcome."""
        return replace(self, gate_passed=passed)


__all__ = [
    "PROMOTION_LADDER",
    "ModelKind",
    "ModelProfile",
    "ModelVersion",
    "Stage",
    "next_stage",
    "prev_stage",
]
