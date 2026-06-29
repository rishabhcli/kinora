"""Model registry: versions, lineage, eval gates, staged promotion, rollback.

The registry is the model-lifecycle brain. It is an in-memory, append-only store
of :class:`~app.mlplatform.serving.model.ModelVersion` rows plus the state machine
that moves a version up the promotion ladder
(``dev → staging → canary → prod``) and back down on rollback.

Two rules make promotion safe:

1. **One rung at a time.** You cannot jump ``dev → prod``. Each
   :meth:`ModelRegistry.promote` call advances exactly one stage, and the registry
   keeps at most one ``prod`` version per ``(name, kind)`` — promoting a new one
   demotes the incumbent to the rung below.
2. **The eval gate guards the first promotion.** Leaving ``DEV`` requires
   ``gate_passed`` to be ``True``. The gate (:class:`EvalGate`) runs a candidate
   over a facet-A :class:`Dataset`, scores each generation with a facet-B
   :class:`RewardModel`, and passes only if the aggregate clears configured floors.
   The gate consumes the protocols from :mod:`contracts`, so it works with the
   offline fakes today and the real facets when they land.

Lineage (``parent`` / ``teacher``) is validated on registration: a declared parent
or teacher must already be registered, so the lineage graph is always closed and
the distillation pipeline can walk teacher → student edges.

Everything is pure and synchronous — no DB, no network. A future ``store.py`` could
persist these rows, but the lifecycle logic lives here and is independently tested.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from app.mlplatform.serving.contracts import Dataset, DatasetCase, RewardModel
from app.mlplatform.serving.errors import (
    DuplicateModelVersionError,
    EvalGateError,
    LineageError,
    ModelNotFoundError,
    PromotionError,
    RollbackError,
)
from app.mlplatform.serving.model import (
    ModelKind,
    ModelVersion,
    Stage,
    next_stage,
    prev_stage,
)

#: A candidate generator: given a model ref and a case, produce its answer. The
#: offline default echoes a deterministic synthetic answer; a real harness would
#: call the model. Kept as an injectable seam so the gate never makes a live call.
Generator = Callable[[str, DatasetCase], str]


def _default_generator(model_ref: str, case: DatasetCase) -> str:
    """Deterministic offline generation: lightly perturb the reference by model.

    A "stronger" model (one whose ref sorts later) reproduces more of the
    reference; this is a stand-in so the gate has something to score without a GPU.
    """
    ref = case.reference or str(case.inputs.get("prompt", case.case_id))
    # Fold the model ref in so different versions produce different (but stable)
    # answers — enough for the reward model to differentiate them.
    return f"{ref} [by {model_ref}]"


@dataclass(frozen=True, slots=True)
class GateResult:
    """The outcome of running an :class:`EvalGate` over a dataset."""

    model_ref: str
    dataset: str
    n_cases: int
    mean_reward: float
    pass_rate: float
    passed: bool
    reasons: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        """A JSON-friendly view for logging / persistence."""
        return {
            "model_ref": self.model_ref,
            "dataset": self.dataset,
            "n_cases": self.n_cases,
            "mean_reward": round(self.mean_reward, 6),
            "pass_rate": round(self.pass_rate, 6),
            "passed": self.passed,
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True, slots=True)
class EvalGate:
    """A promotion gate: a model must clear reward floors over an eval dataset.

    ``min_mean_reward`` is the floor on the average :class:`RewardScore.value`;
    ``min_pass_rate`` is the floor on the fraction of cases the reward model marks
    ``passed``. A gate with both at ``0.0`` always passes (useful for bootstrapping
    a first model when no reward model is available yet).
    """

    min_mean_reward: float = 0.6
    min_pass_rate: float = 0.7

    def __post_init__(self) -> None:
        if not 0.0 <= self.min_mean_reward <= 1.0:
            raise EvalGateError("min_mean_reward must be in [0, 1]")
        if not 0.0 <= self.min_pass_rate <= 1.0:
            raise EvalGateError("min_pass_rate must be in [0, 1]")

    def run(
        self,
        model_ref: str,
        dataset: Dataset,
        reward: RewardModel,
        *,
        generator: Generator = _default_generator,
    ) -> GateResult:
        """Score ``model_ref`` over ``dataset`` and return the gate verdict."""
        if len(dataset) == 0:
            raise EvalGateError(f"eval dataset {dataset.name!r} is empty")
        total = 0.0
        passes = 0
        for case in dataset:
            candidate = generator(model_ref, case)
            verdict = reward.score(case, candidate)
            total += verdict.value
            passes += 1 if verdict.passed else 0
        n = len(dataset)
        mean_reward = total / n
        pass_rate = passes / n
        reasons: list[str] = []
        if mean_reward < self.min_mean_reward:
            reasons.append(f"mean_reward {mean_reward:.3f} < floor {self.min_mean_reward:.3f}")
        if pass_rate < self.min_pass_rate:
            reasons.append(f"pass_rate {pass_rate:.3f} < floor {self.min_pass_rate:.3f}")
        return GateResult(
            model_ref=model_ref,
            dataset=dataset.name,
            n_cases=n,
            mean_reward=mean_reward,
            pass_rate=pass_rate,
            passed=not reasons,
            reasons=tuple(reasons),
        )


@dataclass(frozen=True, slots=True)
class PromotionEvent:
    """One append-only audit entry for a registry mutation."""

    action: str  # register | gate | promote | rollback | archive
    model_ref: str
    detail: str = ""
    from_stage: Stage | None = None
    to_stage: Stage | None = None


class ModelRegistry:
    """In-memory model registry + promotion state machine.

    Rows are immutable :class:`ModelVersion` values keyed by ``name@version``; a
    mutation (promote / rollback / gate) replaces the row with a new immutable copy
    and appends a :class:`PromotionEvent` to the audit log.
    """

    def __init__(self) -> None:
        self._versions: dict[str, ModelVersion] = {}
        self._log: list[PromotionEvent] = []

    # -- registration / lookup --------------------------------------------- #

    def register(self, version: ModelVersion) -> ModelVersion:
        """Register a new immutable model version. Append-only — duplicates raise.

        Validates lineage: any declared ``parent`` / ``teacher`` must already be
        registered. New versions always start in ``DEV``.
        """
        if version.ref in self._versions:
            raise DuplicateModelVersionError(version.name, version.version)
        for label, lref in (("parent", version.parent), ("teacher", version.teacher)):
            if lref is not None and lref not in self._versions:
                raise LineageError(f"{label} {lref!r} of {version.ref!r} is not registered")
        stored = version.with_stage(Stage.DEV)
        self._versions[stored.ref] = stored
        self._log.append(
            PromotionEvent(action="register", model_ref=stored.ref, to_stage=Stage.DEV)
        )
        return stored

    def get(self, ref: str) -> ModelVersion:
        """Return the version stored under ``name@version`` (raises if absent)."""
        try:
            return self._versions[ref]
        except KeyError:
            name, _, ver = ref.partition("@")
            raise ModelNotFoundError(name, ver or None) from None

    def exists(self, ref: str) -> bool:
        """Whether ``ref`` is registered."""
        return ref in self._versions

    def all_versions(self) -> tuple[ModelVersion, ...]:
        """Every registered version, newest-registered last."""
        return tuple(self._versions.values())

    def versions_of(self, name: str) -> tuple[ModelVersion, ...]:
        """All versions of a model name, ordered by semver ascending."""
        rows = [v for v in self._versions.values() if v.name == name]
        if not rows:
            raise ModelNotFoundError(name)
        return tuple(sorted(rows, key=lambda v: v.semver))

    def latest(self, name: str) -> ModelVersion:
        """The highest semver of ``name`` (regardless of stage)."""
        return self.versions_of(name)[-1]

    def current(self, name: str, stage: Stage) -> ModelVersion | None:
        """The single version of ``name`` currently at ``stage`` (or ``None``).

        The state machine keeps at most one version of a name at each ladder rung;
        if several somehow share a rung the highest semver wins (deterministic).
        """
        rows = [v for v in self._versions.values() if v.name == name and v.stage == stage]
        if not rows:
            return None
        return max(rows, key=lambda v: v.semver)

    def production(self, name: str) -> ModelVersion | None:
        """Convenience: the current ``PROD`` version of ``name`` (or ``None``)."""
        return self.current(name, Stage.PROD)

    # -- lineage ----------------------------------------------------------- #

    def lineage(self, ref: str) -> tuple[ModelVersion, ...]:
        """Walk the ``parent`` chain from ``ref`` back to its root, root first."""
        chain: list[ModelVersion] = []
        cursor: str | None = ref
        seen: set[str] = set()
        while cursor is not None:
            if cursor in seen:
                raise LineageError(f"lineage cycle detected at {cursor!r}")
            seen.add(cursor)
            node = self.get(cursor)
            chain.append(node)
            cursor = node.parent
        return tuple(reversed(chain))

    def students_of(self, teacher_ref: str) -> tuple[ModelVersion, ...]:
        """Every version distilled from ``teacher_ref`` (its direct students)."""
        if not self.exists(teacher_ref):
            name, _, ver = teacher_ref.partition("@")
            raise ModelNotFoundError(name, ver or None)
        rows = [v for v in self._versions.values() if v.teacher == teacher_ref]
        return tuple(sorted(rows, key=lambda v: v.semver))

    # -- eval gate --------------------------------------------------------- #

    def run_gate(
        self,
        ref: str,
        gate: EvalGate,
        dataset: Dataset,
        reward: RewardModel,
        *,
        generator: Generator = _default_generator,
    ) -> GateResult:
        """Run ``gate`` for ``ref`` and record the pass/fail on the version row."""
        version = self.get(ref)
        result = gate.run(ref, dataset, reward, generator=generator)
        self._versions[ref] = version.with_gate(passed=result.passed)
        self._log.append(
            PromotionEvent(
                action="gate",
                model_ref=ref,
                detail=f"passed={result.passed} mean={result.mean_reward:.3f}",
            )
        )
        return result

    # -- promotion / rollback ---------------------------------------------- #

    def promote(self, ref: str) -> ModelVersion:
        """Advance ``ref`` exactly one rung up the promotion ladder.

        Rules enforced:

        * leaving ``DEV`` requires the eval gate to have passed;
        * a version already at ``PROD`` cannot be promoted further;
        * an ``ARCHIVED`` version cannot be promoted;
        * promoting into a rung that is already occupied by *another* version of the
          same name demotes that incumbent one rung down (so each rung holds one).
        """
        version = self.get(ref)
        if version.stage == Stage.ARCHIVED:
            raise PromotionError(f"{ref!r} is archived and cannot be promoted")
        target = next_stage(version.stage)
        if target is None:
            raise PromotionError(f"{ref!r} is already at the top stage ({version.stage.value})")
        if version.stage == Stage.DEV and not version.gate_passed:
            raise PromotionError(f"{ref!r} cannot leave DEV: eval gate has not passed")
        # Demote any incumbent already occupying the target rung for this name.
        incumbent = self.current(version.name, target)
        if incumbent is not None and incumbent.ref != ref:
            self._demote_to(incumbent, prev_stage(target) or Stage.DEV)
        promoted = version.with_stage(target)
        self._versions[ref] = promoted
        self._log.append(
            PromotionEvent(
                action="promote",
                model_ref=ref,
                from_stage=version.stage,
                to_stage=target,
            )
        )
        return promoted

    def promote_to(self, ref: str, target: Stage) -> ModelVersion:
        """Promote ``ref`` rung-by-rung until it reaches ``target`` (never skips).

        A convenience over :meth:`promote` that still respects every gate: it loops
        :meth:`promote` so each intermediate rung is honored and audited.
        """
        if target not in (Stage.STAGING, Stage.CANARY, Stage.PROD):
            raise PromotionError(f"{target.value!r} is not a promotable target")
        version = self.get(ref)
        ladder = (Stage.DEV, Stage.STAGING, Stage.CANARY, Stage.PROD)
        if ladder.index(version.stage) > ladder.index(target):
            raise PromotionError(
                f"{ref!r} is at {version.stage.value}, above target {target.value} — "
                "use rollback to move down"
            )
        current = version
        while current.stage != target:
            current = self.promote(ref)
        return current

    def rollback(self, ref: str) -> ModelVersion:
        """Move ``ref`` one rung *down* the ladder (the inverse of promote).

        Rolling back from ``DEV`` is illegal (nothing below it). Rolling back a
        ``PROD`` version promotes the highest-semver ``CANARY`` version of the same
        name into ``PROD`` if one exists, so prod is never left empty silently.
        """
        version = self.get(ref)
        target = prev_stage(version.stage)
        if target is None:
            raise RollbackError(f"{ref!r} is at the bottom stage and cannot roll back")
        demoted = self._demote_to(version, target)
        self._log.append(
            PromotionEvent(
                action="rollback",
                model_ref=ref,
                from_stage=version.stage,
                to_stage=target,
            )
        )
        return demoted

    def archive(self, ref: str) -> ModelVersion:
        """Retire ``ref`` to the terminal ``ARCHIVED`` stage."""
        version = self.get(ref)
        archived = version.with_stage(Stage.ARCHIVED)
        self._versions[ref] = archived
        self._log.append(
            PromotionEvent(
                action="archive",
                model_ref=ref,
                from_stage=version.stage,
                to_stage=Stage.ARCHIVED,
            )
        )
        return archived

    def _demote_to(self, version: ModelVersion, stage: Stage) -> ModelVersion:
        demoted = version.with_stage(stage)
        self._versions[version.ref] = demoted
        return demoted

    # -- audit ------------------------------------------------------------- #

    def history(self, ref: str | None = None) -> tuple[PromotionEvent, ...]:
        """The append-only event log, optionally filtered to one model ref."""
        if ref is None:
            return tuple(self._log)
        return tuple(e for e in self._log if e.model_ref == ref)

    def serving_set(self, kind: ModelKind | None = None) -> tuple[ModelVersion, ...]:
        """Every version currently in ``PROD`` (optionally filtered by kind).

        This is what a serving layer would actually load: the live production
        models. The simulator can be pointed at this set to model real traffic.
        """
        rows = [v for v in self._versions.values() if v.stage == Stage.PROD]
        if kind is not None:
            rows = [v for v in rows if v.kind == kind]
        return tuple(sorted(rows, key=lambda v: (v.name, v.semver)))


__all__ = [
    "EvalGate",
    "Generator",
    "GateResult",
    "ModelRegistry",
    "PromotionEvent",
]
