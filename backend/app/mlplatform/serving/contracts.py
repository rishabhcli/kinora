"""Shared value objects + cross-facet consumption contracts.

This module is the *seam* between facet C (this worktree — registry, distillation,
serving simulator) and the sibling facets A (datasets) and B (reward model). We do
**not** import facet A or B directly: they are built in parallel and may not exist
on disk yet. Instead we define narrow, structural :class:`typing.Protocol`s that
state exactly what facet C needs, plus deterministic offline *fakes* that satisfy
them. When facets A/B land, their concrete types should structurally satisfy these
protocols (a :class:`Dataset` with ``cases`` / a reward scorer with ``score``);
until then the fakes keep this facet fully testable.

Everything here is pure: frozen dataclasses, ``Protocol``s, and a tiny seeded
hash for deterministic fakes. No app imports, no network, no randomness that is
not seed-derived.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

# --------------------------------------------------------------------------- #
# Facet A — datasets
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class DatasetCase:
    """One evaluation/distillation case: a prompt plus an optional reference.

    Mirrors the shape facet A is expected to emit. ``inputs`` is the prompt
    payload handed to a model; ``reference`` is the gold/teacher answer when one
    exists (eval suites have it, raw distillation prompts may not); ``tags`` lets
    callers slice a suite (e.g. by agent role or difficulty).
    """

    case_id: str
    inputs: Mapping[str, object]
    reference: str | None = None
    tags: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Dataset:
    """A named, versioned collection of :class:`DatasetCase`.

    Structurally compatible with facet A's ``Dataset``: callers only rely on
    ``name``, ``version``, and iterating ``cases``.
    """

    name: str
    version: str
    cases: tuple[DatasetCase, ...]
    description: str = ""

    def __post_init__(self) -> None:
        ids = [c.case_id for c in self.cases]
        if len(ids) != len(set(ids)):
            dupes = sorted({i for i in ids if ids.count(i) > 1})
            raise ValueError(f"dataset {self.name!r} has duplicate case ids: {dupes}")

    def __len__(self) -> int:
        return len(self.cases)

    def __iter__(self):  # type: ignore[no-untyped-def]
        return iter(self.cases)

    def filter_tag(self, tag: str) -> Dataset:
        """Return a sub-dataset keeping only cases carrying ``tag``."""
        kept = tuple(c for c in self.cases if tag in c.tags)
        return Dataset(
            name=f"{self.name}:{tag}",
            version=self.version,
            cases=kept,
            description=f"{self.description} (filtered by tag {tag!r})".strip(),
        )


@runtime_checkable
class DatasetSource(Protocol):
    """Facet A's contract: hand out named datasets.

    Facet C consumes this for distillation corpora and eval gates. Any object
    exposing ``get(name)`` and ``names()`` satisfies it.
    """

    def names(self) -> Sequence[str]:
        """Return the names of the datasets this source can produce."""
        ...

    def get(self, name: str) -> Dataset:
        """Return the dataset registered under ``name`` (raises ``KeyError`` if absent)."""
        ...


# --------------------------------------------------------------------------- #
# Facet B — reward model
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class RewardScore:
    """A reward-model verdict on one candidate generation.

    ``value`` is a calibrated quality score in ``[0, 1]`` (higher is better) and
    ``passed`` is the reward model's own accept/reject against its internal
    threshold. ``axes`` optionally breaks the score down per criterion.
    """

    value: float
    passed: bool
    axes: Mapping[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not 0.0 <= self.value <= 1.0:
            raise ValueError(f"reward value must be in [0, 1], got {self.value}")


@runtime_checkable
class RewardModel(Protocol):
    """Facet B's contract: score a candidate generation for a case.

    Facet C consumes this for *eval gates* — a model version only promotes if its
    generations clear a reward bar over an eval dataset.
    """

    def score(self, case: DatasetCase, candidate: str) -> RewardScore:
        """Return the reward verdict for ``candidate`` answering ``case``."""
        ...


# --------------------------------------------------------------------------- #
# Deterministic offline fakes
# --------------------------------------------------------------------------- #


def _seeded_unit(*parts: str) -> float:
    """Deterministic float in [0, 1) from a stable hash of ``parts``.

    Pure and reproducible — the simulator and the fakes use this instead of
    ``random`` so every run with the same inputs is bit-identical.
    """
    h = hashlib.sha256("\x1f".join(parts).encode("utf-8")).digest()
    # Take 8 bytes → 64-bit int → scale to [0, 1).
    n = int.from_bytes(h[:8], "big")
    return n / float(1 << 64)


class StaticDatasetSource:
    """An in-memory :class:`DatasetSource` over a fixed set of datasets.

    The offline stand-in for facet A. Construct it from any datasets you have
    (real facet-A datasets once they exist, or :func:`synthetic_dataset` fixtures).
    """

    def __init__(self, datasets: Sequence[Dataset]) -> None:
        self._by_name: dict[str, Dataset] = {}
        for ds in datasets:
            if ds.name in self._by_name:
                raise ValueError(f"duplicate dataset name {ds.name!r}")
            self._by_name[ds.name] = ds

    def names(self) -> Sequence[str]:
        return tuple(sorted(self._by_name))

    def get(self, name: str) -> Dataset:
        if name not in self._by_name:
            raise KeyError(name)
        return self._by_name[name]


class HeuristicRewardModel:
    """A deterministic offline :class:`RewardModel` (the stand-in for facet B).

    The score is a seeded function of ``(case_id, candidate)`` nudged toward the
    reference when one is present (a candidate that echoes more of the reference's
    tokens scores higher). ``base_quality`` shifts the whole distribution so tests
    can model a "strong teacher" vs. a "weak student". Entirely reproducible.
    """

    def __init__(self, *, threshold: float = 0.6, base_quality: float = 0.0) -> None:
        if not -1.0 <= base_quality <= 1.0:
            raise ValueError("base_quality must be in [-1, 1]")
        self.threshold = threshold
        self.base_quality = base_quality

    def score(self, case: DatasetCase, candidate: str) -> RewardScore:
        seed = _seeded_unit(case.case_id, candidate)
        overlap = _reference_overlap(case.reference, candidate)
        # Blend: 50% intrinsic seed, 50% reference overlap, then shift by base.
        raw = 0.5 * seed + 0.5 * overlap + 0.25 * self.base_quality
        value = min(1.0, max(0.0, raw))
        axes = {"fluency": min(1.0, max(0.0, seed)), "fidelity": overlap}
        return RewardScore(value=value, passed=value >= self.threshold, axes=axes)


def _reference_overlap(reference: str | None, candidate: str) -> float:
    """Jaccard token overlap of ``candidate`` against ``reference`` (0 if no ref)."""
    if not reference:
        return 0.5  # neutral when there is nothing to compare against
    ref_tokens = set(reference.lower().split())
    cand_tokens = set(candidate.lower().split())
    if not ref_tokens and not cand_tokens:
        return 1.0
    union = ref_tokens | cand_tokens
    if not union:
        return 0.0
    return len(ref_tokens & cand_tokens) / len(union)


def synthetic_dataset(name: str, *, size: int, version: str = "1.0.0") -> Dataset:
    """Build a deterministic synthetic eval/distillation dataset of ``size`` cases.

    Useful as a self-contained fixture when facet A is absent. Each case gets a
    stable id, a tiny prompt, a reference answer, and an alternating difficulty
    tag so callers can exercise :meth:`Dataset.filter_tag`.
    """
    if size < 0:
        raise ValueError("size must be non-negative")
    cases = []
    for i in range(size):
        cid = f"{name}-{i:04d}"
        difficulty = "hard" if i % 3 == 0 else "easy"
        prompt = f"beat {i} of {name}: describe the shot"
        reference = f"a wide cinematic shot for beat {i} in {name}"
        cases.append(
            DatasetCase(
                case_id=cid,
                inputs={"prompt": prompt, "index": i},
                reference=reference,
                tags=(difficulty,),
            )
        )
    return Dataset(name=name, version=version, cases=tuple(cases), description="synthetic")


__all__ = [
    "Dataset",
    "DatasetCase",
    "DatasetSource",
    "HeuristicRewardModel",
    "RewardModel",
    "RewardScore",
    "StaticDatasetSource",
    "synthetic_dataset",
]
