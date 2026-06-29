"""Descriptive statistics over a dataset.

A dataset's stats are the at-a-glance health card the API surfaces and the drift
check compares against: how many examples, the role / task / split / label
distributions, the reward and output-length summaries, QA pass rate, the share
carrying director edits, and a scrubbed-coverage figure. Everything is computed
in one pass, pure, JSON-able.

These feed three consumers: the dataset version store records a snapshot into
lineage, :mod:`app.mlplatform.datasets.drift` diffs two snapshots, and the sibling
facets read the role/task mix before training.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from app.mlplatform.datasets.contracts import Dataset, TraceExample


@dataclass(frozen=True, slots=True)
class NumericSummary:
    """Min / max / mean / stdev / quantiles of a numeric series (empty-safe)."""

    count: int
    mean: float
    stdev: float
    minimum: float
    maximum: float
    p50: float
    p90: float

    @classmethod
    def of(cls, values: Iterable[float]) -> NumericSummary:
        xs = sorted(float(v) for v in values)
        if not xs:
            return cls(0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
        return cls(
            count=len(xs),
            mean=round(statistics.fmean(xs), 6),
            stdev=round(statistics.pstdev(xs), 6) if len(xs) > 1 else 0.0,
            minimum=round(xs[0], 6),
            maximum=round(xs[-1], 6),
            p50=round(_quantile(xs, 0.5), 6),
            p90=round(_quantile(xs, 0.9), 6),
        )

    def to_dict(self) -> dict[str, float | int]:
        return {
            "count": self.count,
            "mean": self.mean,
            "stdev": self.stdev,
            "min": self.minimum,
            "max": self.maximum,
            "p50": self.p50,
            "p90": self.p90,
        }


def _quantile(ordered: list[float], q: float) -> float:
    if not ordered:
        return 0.0
    if len(ordered) == 1:
        return ordered[0]
    rank = q * (len(ordered) - 1)
    lo = int(math.floor(rank))
    hi = min(lo + 1, len(ordered) - 1)
    frac = rank - lo
    return ordered[lo] + (ordered[hi] - ordered[lo]) * frac


def _counter(items: Iterable[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for it in items:
        out[it] = out.get(it, 0) + 1
    return out


def _entropy(counts: Mapping[str, int]) -> float:
    """Shannon entropy (bits) of a categorical distribution — a diversity gauge."""
    total = sum(counts.values())
    if total == 0:
        return 0.0
    ent = 0.0
    for c in counts.values():
        if c <= 0:
            continue
        p = c / total
        ent -= p * math.log2(p)
    return round(ent, 6)


@dataclass(frozen=True, slots=True)
class DatasetStats:
    """A one-pass descriptive snapshot of a dataset."""

    name: str
    n: int
    content_hash: str
    role_dist: Mapping[str, int]
    task_dist: Mapping[str, int]
    split_dist: Mapping[str, int]
    label_dist: Mapping[str, int]
    model_dist: Mapping[str, int]
    book_count: int
    session_count: int
    qa_pass_rate: float
    qa_coverage: float
    director_edited_rate: float
    scrubbed_rate: float
    reward: NumericSummary
    output_chars: NumericSummary
    role_entropy: float = 0.0
    meta: Mapping[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "n": self.n,
            "content_hash": self.content_hash,
            "role_dist": dict(self.role_dist),
            "task_dist": dict(self.task_dist),
            "split_dist": dict(self.split_dist),
            "label_dist": dict(self.label_dist),
            "model_dist": dict(self.model_dist),
            "book_count": self.book_count,
            "session_count": self.session_count,
            "qa_pass_rate": round(self.qa_pass_rate, 6),
            "qa_coverage": round(self.qa_coverage, 6),
            "director_edited_rate": round(self.director_edited_rate, 6),
            "scrubbed_rate": round(self.scrubbed_rate, 6),
            "reward": self.reward.to_dict(),
            "output_chars": self.output_chars.to_dict(),
            "role_entropy": self.role_entropy,
            "meta": dict(self.meta),
        }


def _quality_label(ex: TraceExample) -> str | None:
    val = ex.labels.get("quality")
    return str(val) if val is not None else None


def compute_stats(dataset: Dataset) -> DatasetStats:
    """Compute the full descriptive snapshot of a dataset in one pass."""
    exs = dataset.examples
    n = len(exs)
    qa_present = [e for e in exs if e.qa is not None]
    qa_passed = sum(1 for e in qa_present if e.qa is not None and e.qa.passed)
    role_dist = _counter(e.role.value for e in exs)
    labels = [lbl for e in exs if (lbl := _quality_label(e)) is not None]
    return DatasetStats(
        name=dataset.name,
        n=n,
        content_hash=dataset.content_hash,
        role_dist=role_dist,
        task_dist=_counter(e.task.value for e in exs),
        split_dist=_counter(e.split.value for e in exs),
        label_dist=_counter(labels),
        model_dist=_counter(e.model for e in exs),
        book_count=len({e.book_id for e in exs if e.book_id}),
        session_count=len({e.session_id for e in exs if e.session_id}),
        qa_pass_rate=(qa_passed / len(qa_present)) if qa_present else 0.0,
        qa_coverage=(len(qa_present) / n) if n else 0.0,
        director_edited_rate=(sum(1 for e in exs if e.director_edits) / n) if n else 0.0,
        scrubbed_rate=(sum(1 for e in exs if e.scrubbed) / n) if n else 0.0,
        reward=NumericSummary.of(e.reward for e in exs if e.reward is not None),
        output_chars=NumericSummary.of(len(e.output) for e in exs),
        role_entropy=_entropy(role_dist),
        meta=dataset.meta,
    )


__all__ = ["DatasetStats", "NumericSummary", "compute_stats"]
