"""Threshold calibration for the semantic cache.

A semantic cache only returns a stored answer when a new prompt is *similar
enough* to a cached one. "Similar enough" is a cosine threshold, and picking it
by hand is how semantic caches silently start returning wrong answers. This
module derives the threshold from a labelled set of prompt pairs — each pair
tagged *equivalent* (the cached answer is acceptable) or *different* (it is not)
— and chooses the operating point that meets a target precision while keeping as
much recall as possible.

Everything is pure and deterministic: same labelled set in, same threshold out.
No model calls — the caller supplies the cosine scores (typically from the same
:class:`~app.inference.accel.protocol.Embedder` the cache uses).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from .errors import CalibrationError


@dataclass(frozen=True, slots=True)
class LabeledPair:
    """One labelled similarity observation.

    Attributes:
        score: Cosine similarity between the two prompts (``-1..1``; normally
            ``0..1`` for unit text embeddings).
        equivalent: True if returning the cached answer for one given the other
            is *correct* (a true near-duplicate); False if it would be wrong.
    """

    score: float
    equivalent: bool


@dataclass(frozen=True, slots=True)
class CalibrationResult:
    """The chosen threshold plus the confusion stats it achieves on the set."""

    threshold: float
    precision: float
    recall: float
    f1: float
    true_positives: int
    false_positives: int
    false_negatives: int
    true_negatives: int

    @property
    def accepts(self) -> int:
        return self.true_positives + self.false_positives


def _confusion(
    pairs: Sequence[LabeledPair], threshold: float
) -> tuple[int, int, int, int]:
    """Return (tp, fp, fn, tn) for accepting pairs with ``score >= threshold``."""
    tp = fp = fn = tn = 0
    for p in pairs:
        accept = p.score >= threshold
        if p.equivalent and accept:
            tp += 1
        elif not p.equivalent and accept:
            fp += 1
        elif p.equivalent and not accept:
            fn += 1
        else:
            tn += 1
    return tp, fp, fn, tn


def _metrics(tp: int, fp: int, fn: int) -> tuple[float, float, float]:
    precision = tp / (tp + fp) if (tp + fp) else 1.0
    recall = tp / (tp + fn) if (tp + fn) else 1.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0
    return precision, recall, f1


def candidate_thresholds(pairs: Sequence[LabeledPair]) -> list[float]:
    """Distinct thresholds worth testing: just above each observed score, sorted.

    Picking thresholds at midpoints between sorted unique scores (plus the
    extremes) is enough to realize every distinct confusion matrix.
    """
    scores = sorted({p.score for p in pairs})
    if not scores:
        return []
    cands: list[float] = [scores[0] - 1e-9]
    for a, b in zip(scores, scores[1:], strict=False):
        cands.append((a + b) / 2.0)
    cands.append(scores[-1] + 1e-9)
    return cands


def calibrate_threshold(
    pairs: Sequence[LabeledPair],
    *,
    target_precision: float = 0.95,
    min_recall: float = 0.0,
) -> CalibrationResult:
    """Choose the lowest threshold meeting ``target_precision`` (maximising recall).

    Among all thresholds whose precision >= ``target_precision`` *and* recall >=
    ``min_recall``, pick the one with the highest recall (ties broken by higher
    F1, then lower threshold). Lowering the threshold admits more pairs, so the
    minimum qualifying threshold yields the most recall — that is the operating
    point a cache wants (most hits at the required safety).

    Raises:
        CalibrationError: empty set, or no threshold satisfies the targets.
    """
    if not pairs:
        raise CalibrationError("cannot calibrate from an empty labelled set")
    if not (0.0 <= target_precision <= 1.0):
        raise CalibrationError("target_precision must be in [0, 1]")

    best: CalibrationResult | None = None
    for thr in candidate_thresholds(pairs):
        tp, fp, fn, tn = _confusion(pairs, thr)
        precision, recall, f1 = _metrics(tp, fp, fn)
        if precision < target_precision or recall < min_recall:
            continue
        cand = CalibrationResult(
            threshold=round(thr, 6),
            precision=precision,
            recall=recall,
            f1=f1,
            true_positives=tp,
            false_positives=fp,
            false_negatives=fn,
            true_negatives=tn,
        )
        if best is None or _better(cand, best):
            best = cand

    if best is None:
        raise CalibrationError(
            f"no threshold reaches precision>={target_precision} with recall>={min_recall}"
        )
    return best


def _better(a: CalibrationResult, b: CalibrationResult) -> bool:
    """``a`` preferred over ``b``: higher recall, then F1, then lower threshold."""
    if a.recall != b.recall:
        return a.recall > b.recall
    if a.f1 != b.f1:
        return a.f1 > b.f1
    return a.threshold < b.threshold


def evaluate_threshold(
    pairs: Sequence[LabeledPair], threshold: float
) -> CalibrationResult:
    """Score a *given* threshold on a labelled set (no search)."""
    if not pairs:
        raise CalibrationError("cannot evaluate on an empty labelled set")
    tp, fp, fn, tn = _confusion(pairs, threshold)
    precision, recall, f1 = _metrics(tp, fp, fn)
    return CalibrationResult(
        threshold=threshold,
        precision=precision,
        recall=recall,
        f1=f1,
        true_positives=tp,
        false_positives=fp,
        false_negatives=fn,
        true_negatives=tn,
    )


__all__ = [
    "CalibrationResult",
    "LabeledPair",
    "calibrate_threshold",
    "candidate_thresholds",
    "evaluate_threshold",
]
