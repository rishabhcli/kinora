"""Quality filtering + curriculum ordering over a dataset.

Not every example deserves a place in a training set. This module is the
composable predicate layer that selects the subset worth training on and orders
it for a curriculum, all as pure dataset→dataset transforms.

* **Predicates** — small, named, composable filters (`qa_passed`,
  `min_reward`, `is_good_label`, `has_director_edit`, `role_in`, `non_empty`,
  `min_confidence`) combined with :func:`all_of` / :func:`any_of` / :func:`negate`.
* :func:`apply_filter` — run a predicate over a dataset, returning the kept
  dataset + a :class:`FilterReport` (how many each named predicate dropped, for
  observability — the "why is my training set this size" answer).
* **Curriculum** — :func:`order_by_difficulty` sorts examples easy→hard (or the
  reverse) by a difficulty score (default: ``1 − reward`` blended with QA
  failure + director-edit pressure), so the alignment facet can train on a
  curriculum that ramps the hard, heavily-corrected cases last.
* :func:`quality_tiers` — partition a dataset into gold / silver / bronze tiers
  by reward + QA, the coarse quality bands a facet can weight differently.

Pure; no model calls.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum

from app.mlplatform.datasets.contracts import AgentRole, Dataset, TraceExample

#: A predicate keeps an example when it returns True.
Predicate = Callable[[TraceExample], bool]


# --------------------------------------------------------------------------- #
# Composable predicates
# --------------------------------------------------------------------------- #


def qa_passed(ex: TraceExample) -> bool:
    return ex.qa is not None and ex.qa.passed


def has_qa(ex: TraceExample) -> bool:
    return ex.qa is not None


def min_reward(threshold: float) -> Predicate:
    def _p(ex: TraceExample) -> bool:
        return ex.reward is not None and ex.reward >= threshold

    return _p


def max_reward(threshold: float) -> Predicate:
    def _p(ex: TraceExample) -> bool:
        return ex.reward is not None and ex.reward <= threshold

    return _p


def is_good_label(ex: TraceExample) -> bool:
    return ex.labels.get("quality") == "good"


def min_confidence(threshold: float, key: str = "quality_conf") -> Predicate:
    def _p(ex: TraceExample) -> bool:
        conf = ex.weak_labels.get(key)
        return conf is not None and float(conf) >= threshold

    return _p


def has_director_edit(ex: TraceExample) -> bool:
    return bool(ex.director_edits)


def role_in(*roles: AgentRole) -> Predicate:
    allowed = set(roles)

    def _p(ex: TraceExample) -> bool:
        return ex.role in allowed

    return _p


def non_empty(min_chars: int = 1) -> Predicate:
    def _p(ex: TraceExample) -> bool:
        return len((ex.output or "").strip()) >= min_chars

    return _p


def all_of(*predicates: Predicate) -> Predicate:
    def _p(ex: TraceExample) -> bool:
        return all(p(ex) for p in predicates)

    return _p


def any_of(*predicates: Predicate) -> Predicate:
    def _p(ex: TraceExample) -> bool:
        return any(p(ex) for p in predicates)

    return _p


def negate(predicate: Predicate) -> Predicate:
    def _p(ex: TraceExample) -> bool:
        return not predicate(ex)

    return _p


# --------------------------------------------------------------------------- #
# Apply + report
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class FilterReport:
    """How a filter pass thinned a dataset."""

    n_before: int
    n_after: int
    dropped: int
    by_named: dict[str, int] = field(default_factory=dict)

    @property
    def keep_rate(self) -> float:
        return self.n_after / self.n_before if self.n_before else 0.0

    def to_dict(self) -> dict[str, object]:
        return {
            "n_before": self.n_before,
            "n_after": self.n_after,
            "dropped": self.dropped,
            "keep_rate": round(self.keep_rate, 6),
            "by_named": dict(self.by_named),
        }


def apply_filter(
    dataset: Dataset,
    predicate: Predicate,
    *,
    named: dict[str, Predicate] | None = None,
) -> tuple[Dataset, FilterReport]:
    """Filter a dataset by ``predicate``; ``named`` predicates are tallied per-drop.

    ``named`` is an optional diagnostic map ``{name: predicate}`` — for each
    dropped example, every named predicate it *fails* is counted, so the report
    explains which criterion removed how many (predicates may overlap).
    """
    kept: list[TraceExample] = []
    by_named: dict[str, int] = {}
    for ex in dataset.examples:
        if predicate(ex):
            kept.append(ex)
            continue
        if named:
            for name, p in named.items():
                if not p(ex):
                    by_named[name] = by_named.get(name, 0) + 1
    new_ds = Dataset.from_examples(
        f"{dataset.name}:filtered", kept, description=dataset.description, meta=dataset.meta
    )
    return new_ds, FilterReport(
        n_before=len(dataset),
        n_after=len(kept),
        dropped=len(dataset) - len(kept),
        by_named=by_named,
    )


# --------------------------------------------------------------------------- #
# Curriculum
# --------------------------------------------------------------------------- #


def difficulty(ex: TraceExample) -> float:
    """A 0..1 difficulty score (higher = harder), default for the curriculum.

    Blends three signals: low reward is hard, a failed QA verdict is hard, and a
    heavily director-edited output is hard (a human had to correct it a lot).
    """
    base = 1.0 - (ex.reward if ex.reward is not None else 0.5)
    qa_pressure = 0.0 if ex.qa is None else (0.0 if ex.qa.passed else 0.3)
    edit_pressure = min(0.3, 0.15 * len(ex.director_edits))
    return round(min(1.0, base * 0.6 + qa_pressure + edit_pressure), 6)


def order_by_difficulty(
    dataset: Dataset,
    *,
    hardest_last: bool = True,
    score: Callable[[TraceExample], float] = difficulty,
) -> Dataset:
    """Order a dataset as a curriculum (easy→hard by default).

    Ties break on the example id so the order is deterministic.
    """
    ordered = sorted(dataset.examples, key=lambda e: (score(e), e.id), reverse=not hardest_last)
    return Dataset.from_examples(
        f"{dataset.name}:curriculum", ordered, description=dataset.description, meta=dataset.meta
    )


class QualityTier(StrEnum):
    GOLD = "gold"
    SILVER = "silver"
    BRONZE = "bronze"


def tier_of(ex: TraceExample) -> QualityTier:
    """Coarse quality band of an example (gold / silver / bronze)."""
    reward = ex.reward if ex.reward is not None else 0.5
    passed = ex.qa is None or ex.qa.passed
    if passed and reward >= 0.8 and not ex.director_edits:
        return QualityTier.GOLD
    if reward >= 0.5 and passed:
        return QualityTier.SILVER
    return QualityTier.BRONZE


def quality_tiers(dataset: Dataset) -> dict[QualityTier, Dataset]:
    """Partition a dataset into gold / silver / bronze sub-datasets."""
    buckets: dict[QualityTier, list[TraceExample]] = {t: [] for t in QualityTier}
    for ex in dataset.examples:
        buckets[tier_of(ex)].append(ex)
    return {
        tier: Dataset.from_examples(
            f"{dataset.name}:{tier.value}", exs, description=dataset.description, meta=dataset.meta
        )
        for tier, exs in buckets.items()
    }


def golden_subset(dataset: Dataset) -> tuple[Dataset, FilterReport]:
    """The QA-passed, high-reward, unedited subset — the imitation gold set."""
    return apply_filter(
        dataset,
        all_of(qa_passed, min_reward(0.8), negate(has_director_edit), non_empty(4)),
        named={
            "qa_failed_or_missing": qa_passed,
            "low_reward": min_reward(0.8),
            "director_edited": negate(has_director_edit),
            "too_short": non_empty(4),
        },
    )


def _as_predicate_list(predicates: Sequence[Predicate]) -> Predicate:
    return all_of(*predicates)


__all__ = [
    "FilterReport",
    "Predicate",
    "QualityTier",
    "all_of",
    "any_of",
    "apply_filter",
    "difficulty",
    "golden_subset",
    "has_director_edit",
    "has_qa",
    "is_good_label",
    "max_reward",
    "min_confidence",
    "min_reward",
    "negate",
    "non_empty",
    "order_by_difficulty",
    "qa_passed",
    "quality_tiers",
    "role_in",
    "tier_of",
]
