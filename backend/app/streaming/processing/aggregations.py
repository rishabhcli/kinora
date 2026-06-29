"""Incremental aggregate functions and reduce functions.

A windowed or keyed aggregation folds many input records into one output
*without buffering* the inputs: each record updates a small accumulator. This
mirrors Flink's ``AggregateFunction`` (separate ``IN`` / ``ACC`` / ``OUT``
types) and ``ReduceFunction`` (``IN`` == ``OUT``).

The built-ins here — count / sum / min / max / mean — are the vocabulary the
Kinora pipelines need:

* engagement: events-per-window (count), velocity samples (mean), dwell (sum),
* render QA: shots rendered (count), accept rate (mean of a 0/1), p-latency.

All are pure and deterministic; floats are only produced by ``mean`` (a ratio),
never accumulated, so a sum stays exact for integer inputs.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Generic, Protocol, TypeVar

IN = TypeVar("IN")
ACC = TypeVar("ACC")
OUT = TypeVar("OUT")

# Variance-annotated TypeVars for the protocols: the input is consumed
# (contravariant) and the output is produced (covariant); ``ACC`` straddles
# both directions so it must stay invariant.
IN_contra = TypeVar("IN_contra", contravariant=True)
OUT_co = TypeVar("OUT_co", covariant=True)


class AggregateFunction(Protocol[IN_contra, ACC, OUT_co]):
    """Incremental aggregation with separate accumulator and output types.

    ``create_accumulator`` → empty accumulator; ``add`` folds one input;
    ``get_result`` finalizes; ``merge`` combines two accumulators (needed for
    session-window merging and pre-aggregation).
    """

    def create_accumulator(self) -> ACC: ...

    def add(self, value: IN_contra, acc: ACC) -> ACC: ...

    def get_result(self, acc: ACC) -> OUT_co: ...

    def merge(self, a: ACC, b: ACC) -> ACC: ...


#: Combines two values of the same type into one (``IN`` == ``OUT``). A plain
#: callable alias rather than a Protocol, so it composes cleanly where a window
#: reduce needs ``f(T, T) -> T`` without protocol-variance friction.
ReduceFunction = Callable[[IN, IN], IN]


def _identity_float(value: object) -> float:
    """Default field extractor: coerce the record value itself to ``float``."""

    return float(value)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Built-in aggregate functions
# --------------------------------------------------------------------------- #
class CountAggregate(Generic[IN]):
    """Counts records; output is an ``int``."""

    def create_accumulator(self) -> int:
        return 0

    def add(self, value: IN, acc: int) -> int:
        return acc + 1

    def get_result(self, acc: int) -> int:
        return acc

    def merge(self, a: int, b: int) -> int:
        return a + b


@dataclass(slots=True)
class SumAggregate(Generic[IN]):
    """Sums a numeric field extracted from each record."""

    extractor: Callable[[IN], float] = _identity_float

    def create_accumulator(self) -> float:
        return 0.0

    def add(self, value: IN, acc: float) -> float:
        return acc + self.extractor(value)

    def get_result(self, acc: float) -> float:
        return acc

    def merge(self, a: float, b: float) -> float:
        return a + b


@dataclass(slots=True)
class MinAggregate(Generic[IN]):
    """Tracks the minimum of an extracted numeric field."""

    extractor: Callable[[IN], float] = _identity_float

    def create_accumulator(self) -> float | None:
        return None

    def add(self, value: IN, acc: float | None) -> float:
        v = self.extractor(value)
        return v if acc is None else min(acc, v)

    def get_result(self, acc: float | None) -> float | None:
        return acc

    def merge(self, a: float | None, b: float | None) -> float | None:
        if a is None:
            return b
        if b is None:
            return a
        return min(a, b)


@dataclass(slots=True)
class MaxAggregate(Generic[IN]):
    """Tracks the maximum of an extracted numeric field."""

    extractor: Callable[[IN], float] = _identity_float

    def create_accumulator(self) -> float | None:
        return None

    def add(self, value: IN, acc: float | None) -> float:
        v = self.extractor(value)
        return v if acc is None else max(acc, v)

    def get_result(self, acc: float | None) -> float | None:
        return acc

    def merge(self, a: float | None, b: float | None) -> float | None:
        if a is None:
            return b
        if b is None:
            return a
        return max(a, b)


@dataclass(frozen=True, slots=True)
class MeanAccumulator:
    """Running ``(sum, count)`` for an exact incremental mean."""

    total: float = 0.0
    count: int = 0


@dataclass(slots=True)
class MeanAggregate(Generic[IN]):
    """Computes the arithmetic mean of an extracted numeric field.

    Output is ``None`` for an empty window (no records), so a consumer can
    distinguish "no data" from "mean is zero".
    """

    extractor: Callable[[IN], float] = _identity_float

    def create_accumulator(self) -> MeanAccumulator:
        return MeanAccumulator()

    def add(self, value: IN, acc: MeanAccumulator) -> MeanAccumulator:
        return MeanAccumulator(total=acc.total + self.extractor(value), count=acc.count + 1)

    def get_result(self, acc: MeanAccumulator) -> float | None:
        if acc.count == 0:
            return None
        return acc.total / acc.count

    def merge(self, a: MeanAccumulator, b: MeanAccumulator) -> MeanAccumulator:
        return MeanAccumulator(total=a.total + b.total, count=a.count + b.count)


@dataclass(slots=True)
class CollectAggregate(Generic[IN]):
    """Accumulates every input into a list (a buffering aggregate).

    Useful when a window needs the full set of members (e.g. percentile or a
    bespoke reducer over the window contents). Bounded by the window's lifetime.
    """

    def create_accumulator(self) -> list[IN]:
        return []

    def add(self, value: IN, acc: list[IN]) -> list[IN]:
        acc.append(value)
        return acc

    def get_result(self, acc: list[IN]) -> list[IN]:
        return list(acc)

    def merge(self, a: list[IN], b: list[IN]) -> list[IN]:
        return [*a, *b]


def percentile(values: list[float], q: float) -> float | None:
    """Linear-interpolation percentile (``q`` in ``[0, 1]``) of ``values``.

    Pure helper for the render-QA pipeline's p50 / p95 latency. Returns ``None``
    for an empty list. Deterministic — no numpy dependency in the hot path.
    """

    if not values:
        return None
    if not 0.0 <= q <= 1.0:
        raise ValueError("q must be in [0, 1]")
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    pos = q * (len(ordered) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(ordered) - 1)
    frac = pos - lo
    return ordered[lo] + (ordered[hi] - ordered[lo]) * frac
