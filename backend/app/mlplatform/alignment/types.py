"""Typed value objects + the cross-facet ``Dataset`` contract.

Facet A (``app.mlplatform.data``) owns the canonical ``Dataset``. Facet B (this
package) only needs to *consume* it, so we depend on a **structural protocol**
(:class:`DatasetLike`) rather than the concrete class. That keeps the two facets
decoupled — either can land first — while a real ``Dataset`` (which exposes
``samples`` / ``__iter__`` / ``__len__``) satisfies the protocol for free.

The value objects here are the alignment-platform's own vocabulary:

* :class:`Sample` — one director-labelled candidate: a feature vector + a scalar
  reward target (accept = 1 / reject = 0) and optional edit-distance weight.
* :class:`PreferencePair` — a director's pairwise judgement "A ≻ B" over two
  candidates' feature vectors, the substrate for Bradley–Terry / DPO.

Both are frozen, validated on construction, and carry just enough provenance
(``shot_id`` / ``book_id`` / ``source``) to trace a learned signal back to the
episodic record it came from (§9.5).
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from .errors import DataError

# --------------------------------------------------------------------------- #
# Director signal taxonomy
# --------------------------------------------------------------------------- #

#: The raw director signals the platform learns from (§9.5 closed loop). An
#: ``accept`` is the strongest positive; a ``reject`` / ``degrade`` is negative;
#: an ``edit`` is a *weak* negative (the clip was usable but the director changed
#: it), weighted by how large the edit was.
ACCEPT = "accept"
REJECT = "reject"
EDIT = "edit"
DEGRADE = "degrade"

SIGNALS: tuple[str, ...] = (ACCEPT, REJECT, EDIT, DEGRADE)

#: Default mapping from a director signal to its scalar reward label in [0, 1].
SIGNAL_REWARD: dict[str, float] = {
    ACCEPT: 1.0,
    REJECT: 0.0,
    DEGRADE: 0.0,
    EDIT: 0.35,  # usable-but-changed: a soft negative, refined by edit weight
}


def _check_finite(vec: Sequence[float], *, where: str) -> tuple[float, ...]:
    out: list[float] = []
    for i, x in enumerate(vec):
        xf = float(x)
        if math.isnan(xf) or math.isinf(xf):
            raise DataError(f"{where}: non-finite value at index {i}: {x!r}")
        out.append(xf)
    return tuple(out)


@dataclass(frozen=True)
class Sample:
    """One director-labelled training row for the reward model.

    ``features`` is a fixed-width vector of already-measured, normalized signals
    (e.g. the QA axes from §9.5, plus any aesthetic / temporal extras). ``reward``
    is the scalar label in ``[0, 1]`` (1 = the director accepted, 0 = rejected).
    ``weight`` lets edits count as soft negatives proportional to the edit size.
    """

    features: Sequence[float]
    reward: float
    weight: float = 1.0
    signal: str | None = None
    shot_id: str | None = None
    book_id: str | None = None
    source: str | None = None

    def __post_init__(self) -> None:
        feats = _check_finite(self.features, where="Sample.features")
        if not feats:
            raise DataError("Sample.features must be non-empty")
        object.__setattr__(self, "features", feats)
        if math.isnan(self.reward) or math.isinf(self.reward):
            raise DataError(f"Sample.reward must be finite, got {self.reward!r}")
        if not 0.0 <= self.reward <= 1.0:
            raise DataError(f"Sample.reward must be in [0,1], got {self.reward!r}")
        if self.weight < 0.0 or math.isnan(self.weight) or math.isinf(self.weight):
            raise DataError(f"Sample.weight must be finite >= 0, got {self.weight!r}")
        if self.signal is not None and self.signal not in SIGNALS:
            raise DataError(f"Sample.signal must be one of {SIGNALS}, got {self.signal!r}")

    @property
    def dim(self) -> int:
        return len(self.features)

    @classmethod
    def from_signal(
        cls,
        features: Sequence[float],
        signal: str,
        *,
        edit_magnitude: float = 0.0,
        shot_id: str | None = None,
        book_id: str | None = None,
        source: str | None = None,
    ) -> Sample:
        """Build a sample from a raw director signal.

        An ``edit`` signal's reward is pulled toward 0 in proportion to
        ``edit_magnitude`` (0 = trivial tweak → near the soft-negative default;
        1 = wholesale rework → effectively a reject), and its training weight is
        scaled up with the edit size so big edits inform the model more.
        """

        if signal not in SIGNALS:
            raise DataError(f"unknown director signal {signal!r}")
        mag = min(1.0, max(0.0, float(edit_magnitude)))
        if signal == EDIT:
            base = SIGNAL_REWARD[EDIT]
            reward = base * (1.0 - mag)  # bigger edit → closer to reject
            weight = 0.5 + 0.5 * mag
        else:
            reward = SIGNAL_REWARD[signal]
            weight = 1.0
        return cls(
            features=tuple(float(x) for x in features),
            reward=reward,
            weight=weight,
            signal=signal,
            shot_id=shot_id,
            book_id=book_id,
            source=source,
        )


@dataclass(frozen=True)
class PreferencePair:
    """A director's pairwise judgement that the *winner* clip beats the *loser*.

    Both sides are feature vectors of the same width. ``strength`` (in ``(0, 1]``)
    expresses how confident / decisive the preference is — used to weight the
    Bradley–Terry / DPO loss. Identical winner / loser vectors are rejected: a
    pair with no contrast carries no learnable signal.
    """

    winner: Sequence[float]
    loser: Sequence[float]
    strength: float = 1.0
    pair_id: str | None = None
    book_id: str | None = None
    source: str | None = None

    def __post_init__(self) -> None:
        win = _check_finite(self.winner, where="PreferencePair.winner")
        lose = _check_finite(self.loser, where="PreferencePair.loser")
        if not win:
            raise DataError("PreferencePair vectors must be non-empty")
        if len(win) != len(lose):
            raise DataError(
                f"winner/loser dim mismatch: {len(win)} != {len(lose)}"
            )
        if win == lose:
            raise DataError("PreferencePair winner and loser are identical (no signal)")
        object.__setattr__(self, "winner", win)
        object.__setattr__(self, "loser", lose)
        if not 0.0 < self.strength <= 1.0:
            raise DataError(f"strength must be in (0,1], got {self.strength!r}")

    @property
    def dim(self) -> int:
        return len(self.winner)

    def diff(self) -> tuple[float, ...]:
        """``winner - loser`` — the feature contrast Bradley–Terry models."""

        return tuple(w - lz for w, lz in zip(self.winner, self.loser, strict=True))


@runtime_checkable
class DatasetLike(Protocol):
    """Structural contract for facet A's ``Dataset`` (anything iterable of rows).

    The reward / preference learners only ever iterate the dataset and ask for
    its length, so this minimal protocol is all the coupling we need. Facet A's
    concrete ``Dataset`` satisfies it without importing this package.
    """

    def __iter__(self) -> Iterator[object]: ...

    def __len__(self) -> int: ...


@dataclass(frozen=True)
class SampleDataset:
    """A concrete, ordered collection of :class:`Sample` rows.

    A lightweight, self-contained ``Dataset`` so facet B is runnable and testable
    before facet A lands; it also serves as the *adapter target* — see
    :func:`as_sample_dataset`. All rows must share one feature width.
    """

    samples: tuple[Sample, ...]
    name: str = "samples"
    meta: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.samples:
            raise DataError("SampleDataset must be non-empty")
        dim = self.samples[0].dim
        for i, s in enumerate(self.samples):
            if s.dim != dim:
                raise DataError(
                    f"SampleDataset row {i} has dim {s.dim}, expected {dim}"
                )
        object.__setattr__(self, "samples", tuple(self.samples))

    @property
    def dim(self) -> int:
        return self.samples[0].dim

    def __iter__(self) -> Iterator[Sample]:
        return iter(self.samples)

    def __len__(self) -> int:
        return len(self.samples)

    @property
    def rewards(self) -> tuple[float, ...]:
        return tuple(s.reward for s in self.samples)

    @property
    def n_positive(self) -> int:
        return sum(1 for s in self.samples if s.reward >= 0.5)

    @property
    def n_negative(self) -> int:
        return len(self.samples) - self.n_positive


@dataclass(frozen=True)
class PreferenceDataset:
    """An ordered collection of :class:`PreferencePair` rows (one feature width)."""

    pairs: tuple[PreferencePair, ...]
    name: str = "preferences"
    meta: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.pairs:
            raise DataError("PreferenceDataset must be non-empty")
        dim = self.pairs[0].dim
        for i, p in enumerate(self.pairs):
            if p.dim != dim:
                raise DataError(
                    f"PreferenceDataset row {i} has dim {p.dim}, expected {dim}"
                )
        object.__setattr__(self, "pairs", tuple(self.pairs))

    @property
    def dim(self) -> int:
        return self.pairs[0].dim

    def __iter__(self) -> Iterator[PreferencePair]:
        return iter(self.pairs)

    def __len__(self) -> int:
        return len(self.pairs)


def as_sample_dataset(data: object) -> SampleDataset:
    """Adapt any iterable of rows into a :class:`SampleDataset`.

    Accepts an already-built :class:`SampleDataset` (returned as-is), an iterable
    of :class:`Sample`, or facet A's ``Dataset`` whose rows are *duck-typed*: a
    row is accepted if it exposes ``features`` + (``reward`` **or** ``signal``).
    This is the single seam where facet A's data meets facet B's learners.
    """

    if isinstance(data, SampleDataset):
        return data
    if not isinstance(data, Iterable):
        raise DataError(f"cannot adapt non-iterable dataset: {data!r}")
    rows: list[Sample] = []
    for i, row in enumerate(data):
        if isinstance(row, Sample):
            rows.append(row)
            continue
        feats = getattr(row, "features", None)
        if feats is None:
            raise DataError(f"row {i} has no 'features' attribute: {row!r}")
        reward = getattr(row, "reward", None)
        signal = getattr(row, "signal", None)
        if reward is not None:
            rows.append(
                Sample(
                    features=tuple(float(x) for x in feats),
                    reward=float(reward),
                    weight=float(getattr(row, "weight", 1.0)),
                    signal=signal,
                    shot_id=getattr(row, "shot_id", None),
                    book_id=getattr(row, "book_id", None),
                    source=getattr(row, "source", None),
                )
            )
        elif signal is not None:
            rows.append(
                Sample.from_signal(
                    feats,
                    signal,
                    edit_magnitude=float(getattr(row, "edit_magnitude", 0.0)),
                    shot_id=getattr(row, "shot_id", None),
                    book_id=getattr(row, "book_id", None),
                    source=getattr(row, "source", None),
                )
            )
        else:
            raise DataError(f"row {i} has neither 'reward' nor 'signal': {row!r}")
    if not rows:
        raise DataError("cannot adapt an empty dataset")
    return SampleDataset(samples=tuple(rows))
