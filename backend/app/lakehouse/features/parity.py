"""Offline/online parity validation + training-serving skew detection.

Two distinct (often-conflated) checks live here:

1. **Offline/online parity** — for a fixed set of entity keys at a fixed instant,
   does the online store serve the *same* value the offline point-in-time join
   would produce? A mismatch means a materialisation bug, a TTL/clock skew, or a
   transform that differs between the batch and serving paths — the classic
   silent feature-store failure. :func:`check_parity` joins both paths for the
   same keys/instant and reports per-feature agreement.

2. **Training-serving skew** — does the *distribution* of a feature differ between
   the data the model trained on (offline) and the data it sees in production
   (online/live)? Even with perfect parity per row, the live key distribution can
   drift. :func:`detect_skew` computes a per-feature drift score:
   * numeric features → **Population Stability Index (PSI)** over quantile bins,
   * categorical features → an **L-infinity** distance over category frequencies,
   plus simple summary moments, and flags features past a configurable threshold.

Everything is pure (no I/O): the caller gathers the two value sets and hands them
in, so the statistics are deterministic and unit-testable. PSI conventions follow
the usual rule of thumb (``<0.1`` stable, ``0.1–0.25`` moderate, ``>0.25`` large).
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from .types import FeatureSpec, ValueType

# PSI rule-of-thumb thresholds.
PSI_MODERATE = 0.1
PSI_LARGE = 0.25
_EPS = 1e-6


# --------------------------------------------------------------------------- #
# Parity
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class FeatureParity:
    """Per-feature offline vs online agreement over a set of keys."""

    feature: str
    compared: int
    matches: int
    mismatches: tuple[tuple[str, object, object], ...] = ()

    @property
    def match_rate(self) -> float:
        return 1.0 if self.compared == 0 else self.matches / self.compared

    @property
    def ok(self) -> bool:
        return self.matches == self.compared


@dataclass(frozen=True, slots=True)
class ParityReport:
    """Aggregate offline/online parity over the checked features."""

    per_feature: tuple[FeatureParity, ...]

    @property
    def ok(self) -> bool:
        return all(p.ok for p in self.per_feature)

    @property
    def overall_match_rate(self) -> float:
        total = sum(p.compared for p in self.per_feature)
        matched = sum(p.matches for p in self.per_feature)
        return 1.0 if total == 0 else matched / total

    def feature(self, name: str) -> FeatureParity:
        for p in self.per_feature:
            if p.feature == name:
                return p
        raise KeyError(name)


def _as_float_list(value: object) -> list[float] | None:
    """Best-effort coercion of an iterable to a list of floats (``None`` on failure)."""
    if not isinstance(value, (list, tuple)):
        return None
    try:
        return [float(x) for x in value]
    except (TypeError, ValueError):
        return None


def _values_close(a: object, b: object, *, dtype: ValueType, rel_tol: float) -> bool:
    """Type-aware value equality (float-tolerant; order-insensitive for lists)."""
    if a is None or b is None:
        return a is b or a == b
    if dtype == ValueType.FLOAT_VECTOR:
        av, bv = _as_float_list(a), _as_float_list(b)
        if av is None or bv is None:
            return a == b
        if len(av) != len(bv):
            return False
        return all(
            math.isclose(x, y, rel_tol=rel_tol, abs_tol=_EPS) for x, y in zip(av, bv, strict=True)
        )
    if dtype == ValueType.FLOAT:
        try:
            return math.isclose(float(a), float(b), rel_tol=rel_tol, abs_tol=_EPS)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return a == b
    if dtype == ValueType.STRING_LIST:
        if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
            return sorted(map(str, a)) == sorted(map(str, b))
        return a == b
    return a == b


def check_parity(
    specs: Sequence[FeatureSpec],
    *,
    offline: Mapping[str, Mapping[str, object]],
    online: Mapping[str, Mapping[str, object]],
    rel_tol: float = 1e-6,
    max_mismatches: int = 25,
) -> ParityReport:
    """Compare offline vs online feature values per entity key.

    ``offline`` / ``online`` map an entity-key string to that key's ``feature ->
    value`` mapping (the caller produces these from the two stores for the *same*
    keys at the *same* instant). Only keys present in *both* are compared (a key
    materialised but not yet in the offline window, or vice versa, is not a parity
    violation). Per feature: count comparisons, matches, and sample mismatches.
    """
    shared = sorted(set(offline) & set(online))
    by_feature: dict[str, FeatureParity] = {}
    for spec in specs:
        compared = 0
        matches = 0
        mismatches: list[tuple[str, object, object]] = []
        for key in shared:
            off_val = offline[key].get(spec.name)
            on_val = online[key].get(spec.name)
            compared += 1
            if _values_close(off_val, on_val, dtype=spec.dtype, rel_tol=rel_tol):
                matches += 1
            elif len(mismatches) < max_mismatches:
                mismatches.append((key, off_val, on_val))
        by_feature[spec.name] = FeatureParity(
            feature=spec.name,
            compared=compared,
            matches=matches,
            mismatches=tuple(mismatches),
        )
    return ParityReport(per_feature=tuple(by_feature[s.name] for s in specs))


# --------------------------------------------------------------------------- #
# Skew / drift
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class FeatureSkew:
    """Per-feature distribution drift between a reference and a current sample."""

    feature: str
    dtype: ValueType
    method: str  # "psi" | "linf" | "none"
    score: float
    severity: str  # "stable" | "moderate" | "large"
    reference_n: int
    current_n: int
    detail: Mapping[str, float] = field(default_factory=dict)

    @property
    def drifted(self) -> bool:
        return self.severity != "stable"


@dataclass(frozen=True, slots=True)
class SkewReport:
    per_feature: tuple[FeatureSkew, ...]
    threshold: float

    @property
    def drifted_features(self) -> tuple[str, ...]:
        return tuple(s.feature for s in self.per_feature if s.score >= self.threshold)

    @property
    def ok(self) -> bool:
        return not self.drifted_features

    def feature(self, name: str) -> FeatureSkew:
        for s in self.per_feature:
            if s.feature == name:
                return s
        raise KeyError(name)


def _numeric(values: Sequence[object]) -> list[float]:
    out: list[float] = []
    for v in values:
        if v is None:
            continue
        if isinstance(v, bool):
            out.append(1.0 if v else 0.0)
        elif isinstance(v, (int, float)):
            out.append(float(v))
    return out


def _quantile_edges(sample: Sequence[float], bins: int) -> list[float]:
    """Inclusive bin edges from the reference sample's quantiles (deduped)."""
    if not sample:
        return [0.0, 1.0]
    ordered = sorted(sample)
    n = len(ordered)
    edges = [ordered[0]]
    for i in range(1, bins):
        idx = min(n - 1, int(round(i / bins * (n - 1))))
        edges.append(ordered[idx])
    edges.append(ordered[-1])
    # Dedupe while keeping ascending order; widen the last edge so max lands in-bin.
    deduped: list[float] = []
    for e in edges:
        if not deduped or e > deduped[-1]:
            deduped.append(e)
    if len(deduped) < 2:
        deduped = [deduped[0], deduped[0] + 1.0]
    deduped[-1] = math.nextafter(deduped[-1], math.inf)
    return deduped


def _bin_fractions(sample: Sequence[float], edges: Sequence[float]) -> list[float]:
    counts = [0] * (len(edges) - 1)
    for x in sample:
        placed = False
        for i in range(len(edges) - 1):
            if edges[i] <= x < edges[i + 1]:
                counts[i] += 1
                placed = True
                break
        if not placed:  # below first edge → first bin; at/above last → last bin
            counts[0 if x < edges[0] else -1] += 1
    total = sum(counts) or 1
    return [c / total for c in counts]


def population_stability_index(
    reference: Sequence[object], current: Sequence[object], *, bins: int = 10
) -> float:
    """PSI of ``current`` vs a ``reference`` distribution over quantile bins.

    Non-numeric / ``None`` entries are dropped (coerced via :func:`_numeric`), so a
    caller may pass a raw ``Sequence[object]`` feature column.
    """
    ref = _numeric(reference)
    cur = _numeric(current)
    if not ref or not cur:
        return 0.0
    edges = _quantile_edges(ref, bins)
    ref_frac = _bin_fractions(ref, edges)
    cur_frac = _bin_fractions(cur, edges)
    psi = 0.0
    for r, c in zip(ref_frac, cur_frac, strict=True):
        r_adj = max(r, _EPS)
        c_adj = max(c, _EPS)
        psi += (c_adj - r_adj) * math.log(c_adj / r_adj)
    return psi


def categorical_linf(reference: Sequence[object], current: Sequence[object]) -> float:
    """L-infinity distance between the category frequency distributions."""
    ref = Counter(str(v) for v in reference if v is not None)
    cur = Counter(str(v) for v in current if v is not None)
    ref_total = sum(ref.values()) or 1
    cur_total = sum(cur.values()) or 1
    keys = set(ref) | set(cur)
    if not keys:
        return 0.0
    return max(abs(ref[k] / ref_total - cur[k] / cur_total) for k in keys)


def _severity(score: float, *, moderate: float, large: float) -> str:
    if score >= large:
        return "large"
    if score >= moderate:
        return "moderate"
    return "stable"


def detect_skew(
    specs: Sequence[FeatureSpec],
    *,
    reference: Mapping[str, Sequence[object]],
    current: Mapping[str, Sequence[object]],
    bins: int = 10,
    moderate: float = PSI_MODERATE,
    large: float = PSI_LARGE,
) -> SkewReport:
    """Per-feature training-serving skew between a reference and current sample.

    ``reference`` / ``current`` map a feature name to the column of observed values
    in each population (the offline training sample and the live serving sample,
    typically). Numeric/bool features use PSI; string features use the L-infinity
    category distance; vector/bytes features are reported ``method="none"`` (no
    scalar drift score). A feature scoring ``>= moderate`` is flagged.
    """
    out: list[FeatureSkew] = []
    for spec in specs:
        ref_vals = list(reference.get(spec.name, ()))
        cur_vals = list(current.get(spec.name, ()))
        if spec.dtype.is_numeric:
            method = "psi"
            score = population_stability_index(ref_vals, cur_vals, bins=bins)
            detail = {
                "ref_mean": _mean(_numeric(ref_vals)),
                "cur_mean": _mean(_numeric(cur_vals)),
                "ref_std": _std(_numeric(ref_vals)),
                "cur_std": _std(_numeric(cur_vals)),
            }
        elif spec.dtype.is_categorical:
            method = "linf"
            score = categorical_linf(ref_vals, cur_vals)
            detail = {"ref_cardinality": float(len(set(map(str, ref_vals))))}
        else:
            method = "none"
            score = 0.0
            detail = {}
        out.append(
            FeatureSkew(
                feature=spec.name,
                dtype=spec.dtype,
                method=method,
                score=score,
                severity=_severity(score, moderate=moderate, large=large),
                reference_n=len(ref_vals),
                current_n=len(cur_vals),
                detail=detail,
            )
        )
    return SkewReport(per_feature=tuple(out), threshold=moderate)


def _mean(xs: Sequence[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _std(xs: Sequence[float]) -> float:
    if len(xs) < 2:
        return 0.0
    m = _mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


__all__ = [
    "FeatureParity",
    "FeatureSkew",
    "ParityReport",
    "SkewReport",
    "categorical_linf",
    "check_parity",
    "detect_skew",
    "population_stability_index",
]
