"""Deterministic sampling + class balancing over a dataset.

A raw trace corpus is skewed: most shots pass QA, a few roles dominate, rewards
cluster near 1.0. Training on it as-is teaches the model the prior, not the
signal. This module reshapes a :class:`Dataset` into a sampled one with a chosen
balance, **deterministically** (a stable per-example hash seeded by a caller
seed decides ties + subsampling order — same dataset + seed → same sample), so a
training run is reproducible.

Strategies:

* :func:`subsample` — take ``k`` examples (or a fraction) uniformly at random
  (deterministic), preserving nothing but size.
* :func:`balance_by` — class-balance on a key (role / label / QA verdict): cap or
  upsample each class toward the target count so no class dominates. Supports
  ``UNDERSAMPLE`` (cap the majority), ``OVERSAMPLE`` (repeat the minority), and
  ``TARGET`` (every class to an exact n).
* :func:`weighted_sample` — sample with probability proportional to a per-example
  weight (default: the reward), so high-reward examples are seen more often
  without discarding the tail.
* :func:`stratified_subsample` — subsample while preserving each stratum's *share*
  (a proportional down-sample, e.g. for a quick smoke dataset that still mirrors
  the full mix).

Oversampling repeats examples; because :class:`Dataset` requires unique ids, a
repeat is materialised as a distinct example with a ``#k`` id suffix and a
``resampled`` flag in its labels, so the lineage stays honest. Pure; no I/O.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from functools import partial

from app.mlplatform.datasets.contracts import Dataset, TraceExample
from app.mlplatform.datasets.errors import DatasetError

#: A key function partitioning examples into classes for balancing.
ClassKey = Callable[[TraceExample], str]
#: A weight function (non-negative) for weighted sampling.
WeightFn = Callable[[TraceExample], float]


def role_key(ex: TraceExample) -> str:
    return ex.role.value


def label_key(ex: TraceExample) -> str:
    return str(ex.labels.get("quality", "unlabeled"))


def qa_key(ex: TraceExample) -> str:
    if ex.qa is None:
        return "no_qa"
    return "pass" if ex.qa.passed else "fail"


def reward_weight(ex: TraceExample) -> float:
    return ex.reward if ex.reward is not None else 0.5


class BalanceMode(StrEnum):
    UNDERSAMPLE = "undersample"  #: cap every class at the *minority* count
    OVERSAMPLE = "oversample"  #: repeat every class up to the *majority* count
    TARGET = "target"  #: every class to an exact target count


def _rank(key: str, seed: int) -> float:
    """A stable [0,1) rank for an example key under a seed (deterministic order)."""
    digest = hashlib.sha256(f"{seed}:{key}".encode()).digest()
    return int.from_bytes(digest[:8], "big") / 2**64


def _ordered(examples: Sequence[TraceExample], seed: int) -> list[TraceExample]:
    """Examples in a deterministic, seed-shuffled order."""
    return sorted(examples, key=lambda e: _rank(e.id, seed))


def _resampled(ex: TraceExample, copy_index: int) -> TraceExample:
    """A distinct copy of an example for oversampling (unique id + a flag)."""
    if copy_index == 0:
        return ex
    return replace(
        ex,
        id=f"{ex.id}#r{copy_index}",
        labels={**ex.labels, "resampled": copy_index},
    )


@dataclass(frozen=True, slots=True)
class SampleReport:
    """What a sampling pass produced (class counts before/after)."""

    before: dict[str, int]
    after: dict[str, int]
    n_before: int
    n_after: int

    def to_dict(self) -> dict[str, object]:
        return {
            "before": dict(self.before),
            "after": dict(self.after),
            "n_before": self.n_before,
            "n_after": self.n_after,
        }


def subsample(
    dataset: Dataset, *, k: int | None = None, fraction: float | None = None, seed: int = 1729
) -> Dataset:
    """Take ``k`` (or ``fraction``) examples uniformly at random (deterministic)."""
    n = len(dataset)
    if k is None and fraction is None:
        raise DatasetError("subsample needs either k or fraction")
    if fraction is not None:
        if not 0.0 < fraction <= 1.0:
            raise DatasetError("fraction must be in (0, 1]")
        k = max(1, round(n * fraction)) if n else 0
    assert k is not None
    kept = _ordered(dataset.examples, seed)[: min(k, n)]
    return Dataset.from_examples(
        f"{dataset.name}:sample", kept, description=dataset.description, meta=dataset.meta
    )


def _class_counts(examples: Sequence[TraceExample], key: ClassKey) -> dict[str, int]:
    counts: dict[str, int] = {}
    for ex in examples:
        counts[key(ex)] = counts.get(key(ex), 0) + 1
    return counts


def balance_by(
    dataset: Dataset,
    key: ClassKey = role_key,
    *,
    mode: BalanceMode = BalanceMode.UNDERSAMPLE,
    target: int | None = None,
    seed: int = 1729,
) -> tuple[Dataset, SampleReport]:
    """Class-balance a dataset on ``key`` toward an even (or ``target``) count."""
    if not dataset.examples:
        raise DatasetError("cannot balance an empty dataset")

    by_class: dict[str, list[TraceExample]] = {}
    for ex in dataset.examples:
        by_class.setdefault(key(ex), []).append(ex)
    before = {k: len(v) for k, v in by_class.items()}

    if mode is BalanceMode.TARGET:
        if target is None:
            raise DatasetError("TARGET mode needs a target count")
        per_class = target
    elif mode is BalanceMode.UNDERSAMPLE:
        per_class = min(before.values())
    else:  # OVERSAMPLE
        per_class = max(before.values())

    out: list[TraceExample] = []
    for members in by_class.values():
        ordered = _ordered(members, seed)
        if per_class <= len(ordered):
            out.extend(ordered[:per_class])
        else:
            # repeat (round-robin) to reach per_class, with unique resampled ids
            for i in range(per_class):
                base = ordered[i % len(ordered)]
                copy_index = i // len(ordered)
                out.append(_resampled(base, copy_index))

    # Re-order the merged set deterministically so classes interleave.
    out = _ordered(out, seed)
    sampled = Dataset.from_examples(
        f"{dataset.name}:balanced", out, description=dataset.description, meta=dataset.meta
    )
    after = _class_counts(out, key)
    return sampled, SampleReport(
        before=before, after=after, n_before=len(dataset), n_after=len(out)
    )


def weighted_sample(
    dataset: Dataset,
    *,
    k: int,
    weight: WeightFn = reward_weight,
    seed: int = 1729,
    with_replacement: bool = True,
) -> Dataset:
    """Sample ``k`` examples with probability ∝ ``weight`` (deterministic).

    Uses the Efraimidis–Spirakis A-Res keying: each example gets a key
    ``u**(1/w)`` from a stable per-example uniform ``u``; the top-``k`` keys win.
    With replacement, the keys are recomputed per draw with a draw-indexed seed.
    """
    n = len(dataset)
    if n == 0:
        raise DatasetError("cannot sample an empty dataset")
    if k <= 0:
        raise DatasetError("k must be positive")

    def _u(ex: TraceExample, draw: int) -> float:
        return max(_rank(f"{ex.id}:{draw}", seed), 1e-12)

    if not with_replacement:
        keyed = sorted(
            dataset.examples,
            key=lambda e: _u(e, 0) ** (1.0 / max(weight(e), 1e-9)),
            reverse=True,
        )
        kept = keyed[: min(k, n)]
        return Dataset.from_examples(
            f"{dataset.name}:weighted", kept, description=dataset.description, meta=dataset.meta
        )

    # With replacement: k independent weighted draws (max-key per draw).
    def _draw_key(draw: int, ex: TraceExample) -> float:
        return _u(ex, draw) ** (1.0 / max(weight(ex), 1e-9))

    out: list[TraceExample] = []
    for draw in range(k):
        best = max(dataset.examples, key=partial(_draw_key, draw))
        out.append(_resampled(best, draw if any(o.id == best.id for o in out) else 0))
    # Ensure ids stay unique (collisions become resampled copies).
    seen: dict[str, int] = {}
    unique: list[TraceExample] = []
    for ex in out:
        c = seen.get(ex.id, 0)
        seen[ex.id] = c + 1
        unique.append(_resampled(ex, c) if c else ex)
    return Dataset.from_examples(
        f"{dataset.name}:weighted", unique, description=dataset.description, meta=dataset.meta
    )


def stratified_subsample(
    dataset: Dataset, *, fraction: float, key: ClassKey = role_key, seed: int = 1729
) -> Dataset:
    """Down-sample to ``fraction`` while preserving each stratum's share."""
    if not 0.0 < fraction <= 1.0:
        raise DatasetError("fraction must be in (0, 1]")
    by_class: dict[str, list[TraceExample]] = {}
    for ex in dataset.examples:
        by_class.setdefault(key(ex), []).append(ex)
    out: list[TraceExample] = []
    for members in by_class.values():
        take = max(1, round(len(members) * fraction)) if members else 0
        out.extend(_ordered(members, seed)[:take])
    out = _ordered(out, seed)
    return Dataset.from_examples(
        f"{dataset.name}:stratsample", out, description=dataset.description, meta=dataset.meta
    )


__all__ = [
    "BalanceMode",
    "ClassKey",
    "SampleReport",
    "WeightFn",
    "balance_by",
    "label_key",
    "qa_key",
    "reward_weight",
    "role_key",
    "stratified_subsample",
    "subsample",
    "weighted_sample",
]
