"""The side-by-side comparison collector — a paired-sample dataset of renders.

For every shadow-sampled shot the harness produces *two* outcomes for the same
:class:`~app.video.shadow.seams.ShotSpec`: the **production** render (what the
reader saw / would have seen) and the **candidate** render. This module records
them as a :class:`PairedSample` and accumulates a :class:`ComparisonDataset` that
the §-stats layer consumes.

The pairing is by ``shot_id`` so the analysis can run a *paired* test (each shot is
its own control), which is far more powerful than comparing two unpaired means —
shot difficulty varies wildly, and pairing cancels that nuisance variance.

Pure data + bookkeeping; no I/O, no clock. Serialisable (pydantic v2) so a dataset
can be persisted to object storage and re-loaded for offline analysis or replay.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Sequence

from pydantic import BaseModel, ConfigDict, Field

from .seams import RenderOutcome, ShotSpec


class PairedSample(BaseModel):
    """One shot rendered on both models — the unit of paired comparison."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    shot_id: str
    production: RenderOutcome
    candidate: RenderOutcome
    #: The spec both renders were produced from (kept for replay + audit).
    spec: ShotSpec

    # -- derived per-sample views ---------------------------------------- #

    @property
    def both_succeeded(self) -> bool:
        """True iff *both* models produced a usable clip (the comparable case)."""
        return self.production.succeeded and self.candidate.succeeded

    @property
    def either_gated(self) -> bool:
        """True iff either side was refused by a spend gate (a non-fault)."""
        return self.production.is_gated or self.candidate.is_gated

    @property
    def quality_delta(self) -> float | None:
        """``candidate.quality - production.quality`` when both are scored.

        ``None`` unless both renders succeeded *and* carry a quality score — the
        analysis only pairs comparable shots.
        """
        if not self.both_succeeded:
            return None
        if self.candidate.quality is None or self.production.quality is None:
            return None
        return self.candidate.quality - self.production.quality

    @property
    def cost_delta(self) -> float:
        """``candidate.video_seconds - production.video_seconds`` (signed)."""
        return self.candidate.video_seconds - self.production.video_seconds

    @property
    def latency_delta_ms(self) -> float:
        """``candidate.latency_ms - production.latency_ms`` (signed)."""
        return self.candidate.latency_ms - self.production.latency_ms


class ComparisonDataset(BaseModel):
    """An accumulating, serialisable collection of :class:`PairedSample`.

    De-duplicates by ``shot_id`` (last write wins) so a retried shadow render
    updates rather than double-counts its sample.
    """

    model_config = ConfigDict(extra="forbid")

    candidate_model: str
    production_model: str
    samples: list[PairedSample] = Field(default_factory=list)

    def add(self, sample: PairedSample) -> None:
        """Append (or replace, by ``shot_id``) a paired sample."""
        for index, existing in enumerate(self.samples):
            if existing.shot_id == sample.shot_id:
                self.samples[index] = sample
                return
        self.samples.append(sample)

    def extend(self, samples: Iterable[PairedSample]) -> None:
        """Add many samples (each de-duplicated by ``shot_id``)."""
        for sample in samples:
            self.add(sample)

    def __iter__(self) -> Iterator[PairedSample]:  # type: ignore[override]
        return iter(self.samples)

    def __len__(self) -> int:
        return len(self.samples)

    # -- paired vectors for the stats layer ------------------------------ #

    def comparable(self) -> list[PairedSample]:
        """Samples where both models succeeded and both are scored.

        These are the only samples a paired *quality* test may use.
        """
        return [s for s in self.samples if s.quality_delta is not None]

    def quality_deltas(self) -> list[float]:
        """Per-shot ``candidate - production`` quality deltas (comparable only)."""
        return [delta for s in self.comparable() if (delta := s.quality_delta) is not None]

    def paired_qualities(self) -> tuple[list[float], list[float]]:
        """``(production_qualities, candidate_qualities)`` over comparable samples.

        Aligned element-wise (same shot at each index) so a paired test can index
        them together.
        """
        prod: list[float] = []
        cand: list[float] = []
        for sample in self.comparable():
            assert sample.production.quality is not None  # noqa: S101 - guarded by comparable()
            assert sample.candidate.quality is not None  # noqa: S101
            prod.append(sample.production.quality)
            cand.append(sample.candidate.quality)
        return prod, cand

    def cost_deltas(self) -> list[float]:
        """Per-shot video-second cost deltas over *both-succeeded* samples."""
        return [s.cost_delta for s in self.samples if s.both_succeeded]

    def latency_deltas_ms(self) -> list[float]:
        """Per-shot latency deltas (ms) over *both-succeeded* samples."""
        return [s.latency_delta_ms for s in self.samples if s.both_succeeded]


class FailureTally(BaseModel):
    """Per-model failure accounting over a dataset (gated renders excluded)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    attempts: int
    successes: int
    gated: int
    failures_by_kind: dict[str, int]

    @property
    def scored_attempts(self) -> int:
        """Attempts that were not refused by a spend gate (the real denominator)."""
        return self.attempts - self.gated

    @property
    def failure_rate(self) -> float:
        """Fraction of *non-gated* attempts that failed (``0`` if none)."""
        denom = self.scored_attempts
        if denom <= 0:
            return 0.0
        return (denom - self.successes) / denom


def _tally(outcomes: Sequence[RenderOutcome]) -> FailureTally:
    """Build a :class:`FailureTally` from a sequence of one model's outcomes."""
    by_kind: dict[str, int] = {}
    gated = 0
    successes = 0
    for outcome in outcomes:
        if outcome.is_gated:
            gated += 1
            continue
        if outcome.succeeded:
            successes += 1
        else:
            key = outcome.failure.value
            by_kind[key] = by_kind.get(key, 0) + 1
    return FailureTally(
        attempts=len(outcomes),
        successes=successes,
        gated=gated,
        failures_by_kind=by_kind,
    )


def production_failures(dataset: ComparisonDataset) -> FailureTally:
    """Failure accounting for the production model across the dataset."""
    return _tally([s.production for s in dataset.samples])


def candidate_failures(dataset: ComparisonDataset) -> FailureTally:
    """Failure accounting for the candidate model across the dataset."""
    return _tally([s.candidate for s in dataset.samples])


__all__ = [
    "ComparisonDataset",
    "FailureTally",
    "PairedSample",
    "candidate_failures",
    "production_failures",
]
