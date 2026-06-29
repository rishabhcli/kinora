"""Knowledge-distillation pipeline: teacher → student, end to end (simulated).

Distillation compresses a big *teacher* model into a small, cheap *student* by
training the student to imitate the teacher's outputs on a corpus of prompts. In a
serving platform this is how you make read-ahead generation affordable: distil the
70B reasoning brain into a 7B student that serves at a fraction of the cost while
holding most of the quality.

This module models the full pipeline deterministically and offline:

1. **Dataset generation** — for each prompt in a facet-A :class:`Dataset`, the
   teacher (a registered :class:`ModelVersion` + a generator) produces a target
   answer, yielding :class:`TeacherStudentExample` rows. Optional reward filtering
   (facet B) keeps only examples the reward model judges good enough — distilling
   from clean labels is the difference between a useful student and a noisy one.
2. **Training orchestration** — a *simulated* training loop: the student's modeled
   quality rises along a saturating learning curve toward a fraction of the
   teacher's quality (capacity-limited), while its serving profile is derived from
   the teacher's by the requested compression ratio (smaller params → cheaper
   per-token decode, smaller KV footprint). No gradients, no GPU.
3. **Registration** — the result is a fresh :class:`ModelVersion` carrying lineage
   (``teacher=`` the teacher ref) ready to drop into the registry and face its eval
   gate before promotion.

Everything is reproducible from the spec; the same spec always yields the same
student profile and the same example corpus.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass

from app.mlplatform.serving.contracts import (
    Dataset,
    DatasetCase,
    RewardModel,
    _seeded_unit,
)
from app.mlplatform.serving.errors import (
    DistillationSpecError,
    EmptyCorpusError,
    LineageError,
)
from app.mlplatform.serving.model import ModelProfile, ModelVersion, Stage
from app.mlplatform.serving.registry import ModelRegistry

#: Teacher generator seam: given the teacher ref and a case, produce a target answer.
TeacherGenerator = Callable[[str, DatasetCase], str]


def _default_teacher_generator(teacher_ref: str, case: DatasetCase) -> str:
    """Deterministic teacher labels: a polished expansion of the reference."""
    base = case.reference or str(case.inputs.get("prompt", case.case_id))
    return f"{base} — rendered with cinematic detail [{teacher_ref}]"


@dataclass(frozen=True, slots=True)
class TeacherStudentExample:
    """One distillation training row: a prompt and the teacher's target output."""

    case_id: str
    prompt: str
    teacher_output: str
    teacher_reward: float
    kept: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "case_id": self.case_id,
            "prompt": self.prompt,
            "teacher_output": self.teacher_output,
            "teacher_reward": round(self.teacher_reward, 6),
            "kept": self.kept,
        }


@dataclass(frozen=True, slots=True)
class DistillationSpec:
    """The recipe for one distillation run.

    * ``student_name`` / ``student_version`` — identity of the model produced.
    * ``teacher_ref`` — ``name@version`` of the registered teacher.
    * ``compression`` — student params ÷ teacher params (e.g. ``0.1`` = 10× smaller).
    * ``quality_retention`` — the asymptotic fraction of the teacher's quality the
      student can reach given its capacity (a smaller student retains less).
    * ``epochs`` — simulated training epochs; quality follows a saturating curve so
      more epochs help with diminishing returns.
    * ``reward_floor`` — drop teacher examples scoring below this (label cleaning);
      ``0.0`` keeps everything.
    """

    student_name: str
    student_version: str
    teacher_ref: str
    compression: float = 0.1
    quality_retention: float = 0.9
    epochs: int = 3
    reward_floor: float = 0.0

    def __post_init__(self) -> None:
        if not self.student_name:
            raise DistillationSpecError("student_name must be non-empty")
        if not 0.0 < self.compression < 1.0:
            raise DistillationSpecError("compression must be in (0, 1)")
        if not 0.0 < self.quality_retention <= 1.0:
            raise DistillationSpecError("quality_retention must be in (0, 1]")
        if self.epochs < 1:
            raise DistillationSpecError("epochs must be >= 1")
        if not 0.0 <= self.reward_floor <= 1.0:
            raise DistillationSpecError("reward_floor must be in [0, 1]")


@dataclass(frozen=True, slots=True)
class DistillationResult:
    """The product of a distillation run."""

    student: ModelVersion
    teacher_ref: str
    n_examples_total: int
    n_examples_kept: int
    examples: tuple[TeacherStudentExample, ...]
    modeled_student_quality: float
    teacher_mean_reward: float

    @property
    def keep_ratio(self) -> float:
        if self.n_examples_total == 0:
            return 0.0
        return self.n_examples_kept / self.n_examples_total

    def as_dict(self) -> dict[str, object]:
        return {
            "student": self.student.ref,
            "teacher": self.teacher_ref,
            "n_examples_total": self.n_examples_total,
            "n_examples_kept": self.n_examples_kept,
            "keep_ratio": round(self.keep_ratio, 4),
            "modeled_student_quality": round(self.modeled_student_quality, 6),
            "teacher_mean_reward": round(self.teacher_mean_reward, 6),
        }


def _learning_curve(retention: float, epochs: int) -> float:
    """Saturating training curve → fraction of teacher quality reached.

    ``1 - 2^-epochs`` saturates toward 1 with diminishing returns, scaled by the
    capacity ceiling ``retention``. Deterministic, monotonic in epochs.
    """
    progress = 1.0 - 2.0 ** (-float(epochs))
    return retention * progress


def _student_profile(teacher: ModelProfile, compression: float) -> ModelProfile:
    """Derive the student's serving profile from the teacher's by compression.

    A model ``c×`` the parameter count decodes faster and uses less KV per token. We
    model decode/prefill cost and KV footprint as scaling with the compression
    ratio (sub-linearly for compute via a sqrt, linearly for memory), and cost per
    token proportionally to compute. These are honest first-order approximations for
    a capacity-planning simulation, not a calibrated kernel model.
    """
    compute_scale = math.sqrt(compression)  # compute is between linear and constant
    return ModelProfile(
        decode_ms_per_token=max(1e-3, teacher.decode_ms_per_token * compute_scale),
        prefill_ms_per_token=max(1e-3, teacher.prefill_ms_per_token * compute_scale),
        kv_bytes_per_token=max(1, int(round(teacher.kv_bytes_per_token * compression))),
        params_billions=max(1e-3, teacher.params_billions * compression),
        cost_per_1k_tokens=teacher.cost_per_1k_tokens * compute_scale,
        batch_overhead_ms_per_seq=teacher.batch_overhead_ms_per_seq,
        accept_rate=teacher.accept_rate,
        max_context_tokens=teacher.max_context_tokens,
    )


class DistillationPipeline:
    """Runs a :class:`DistillationSpec` against a registry + a teacher generator.

    The pipeline reads the teacher from the registry (validating lineage), generates
    a teacher-labeled corpus over a dataset, optionally reward-filters it, simulates
    training to a modeled quality, derives the student profile, and registers the
    student with ``teacher=`` lineage. The returned :class:`DistillationResult` is
    everything a caller needs to then run the eval gate and promote.
    """

    def __init__(
        self,
        registry: ModelRegistry,
        *,
        teacher_generator: TeacherGenerator = _default_teacher_generator,
    ) -> None:
        self.registry = registry
        self._teacher_generator = teacher_generator

    def generate_corpus(
        self,
        spec: DistillationSpec,
        dataset: Dataset,
        *,
        reward: RewardModel | None = None,
    ) -> list[TeacherStudentExample]:
        """Produce teacher-labeled examples over ``dataset`` (optionally filtered)."""
        if not self.registry.exists(spec.teacher_ref):
            raise LineageError(f"teacher {spec.teacher_ref!r} is not registered")
        if len(dataset) == 0:
            raise EmptyCorpusError(f"distillation dataset {dataset.name!r} is empty")
        examples: list[TeacherStudentExample] = []
        for case in dataset:
            prompt = str(case.inputs.get("prompt", case.case_id))
            output = self._teacher_generator(spec.teacher_ref, case)
            # No judge → trust the teacher's label outright.
            score = reward.score(case, output).value if reward is not None else 1.0
            kept = score >= spec.reward_floor
            examples.append(
                TeacherStudentExample(
                    case_id=case.case_id,
                    prompt=prompt,
                    teacher_output=output,
                    teacher_reward=score,
                    kept=kept,
                )
            )
        return examples

    def distill(
        self,
        spec: DistillationSpec,
        dataset: Dataset,
        *,
        reward: RewardModel | None = None,
        register: bool = True,
    ) -> DistillationResult:
        """Run the full pipeline and (by default) register the resulting student."""
        teacher = self.registry.get(spec.teacher_ref)
        corpus = self.generate_corpus(spec, dataset, reward=reward)
        kept = [e for e in corpus if e.kept]
        if not kept:
            raise EmptyCorpusError(
                f"no examples survived the reward_floor {spec.reward_floor} filter"
            )
        teacher_mean = sum(e.teacher_reward for e in corpus) / len(corpus)

        # Simulated training: student quality = teacher_quality * curve, where the
        # teacher's effective quality is its mean label reward, modulated by how much
        # clean data survived filtering (more kept data → closer to the ceiling).
        curve = _learning_curve(spec.quality_retention, spec.epochs)
        data_factor = 0.8 + 0.2 * (len(kept) / len(corpus))  # in [0.8, 1.0]
        # A tiny seeded jitter keeps distinct specs distinguishable without breaking
        # determinism (same spec → same jitter).
        jitter = (_seeded_unit(spec.student_name, spec.student_version) - 0.5) * 0.02
        modeled_quality = max(0.0, min(1.0, teacher_mean * curve * data_factor + jitter))

        student_profile = _student_profile(teacher.profile, spec.compression)
        student = ModelVersion(
            name=spec.student_name,
            version=spec.student_version,
            # A distilled student serves the same role as its teacher.
            kind=teacher.kind,
            profile=student_profile,
            stage=Stage.DEV,
            teacher=spec.teacher_ref,
            tags=("distilled",),
            metadata={
                "compression": f"{spec.compression}",
                "epochs": f"{spec.epochs}",
                "modeled_quality": f"{modeled_quality:.6f}",
            },
        )
        if register:
            student = self.registry.register(student)

        return DistillationResult(
            student=student,
            teacher_ref=spec.teacher_ref,
            n_examples_total=len(corpus),
            n_examples_kept=len(kept),
            examples=tuple(corpus),
            modeled_student_quality=modeled_quality,
            teacher_mean_reward=teacher_mean,
        )


__all__ = [
    "DistillationPipeline",
    "DistillationResult",
    "DistillationSpec",
    "TeacherGenerator",
    "TeacherStudentExample",
]
