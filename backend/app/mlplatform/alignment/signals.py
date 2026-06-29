"""Turn raw director episodic events into reward / preference training data (§9.5).

The §9.5 closed loop writes every QA verdict to episodic memory: each rendered
shot carries its four QA axes (CCS / style / timeline / motion) plus the
director's disposition — *accepted*, *rejected*, *degraded*, or *edited* (a region
comment that regenerated the shot). This module is the deterministic, side-effect
free **ingestion seam** that converts those events into the platform's vocabulary:

* :func:`sample_from_event` — one :class:`DirectorEvent` → one :class:`Sample`,
  normalizing the raw QA axes into the 0..1 "goodness" feature space (CCS passes
  through, style-drift / motion-artifact are inverted, the timeline boolean maps
  to 1/0, optional aesthetic / temporal axes pass through).
* :func:`build_sample_dataset` — a stream of events → a :class:`SampleDataset`.
* :func:`pairs_from_events` — derive :class:`PreferencePair`s by contrasting an
  accepted shot against a rejected / degraded shot **of the same beat** (the only
  honest pairwise comparison: same intended content, different outcome).

Feature order matches :data:`FEATURE_NAMES` so a model trained here scores live QA
records directly. Nothing here calls a model, touches a DB, or spends a credit; it
operates purely on already-measured numbers, mirroring `app/render/reward.py`'s
discipline while staying in the platform package.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from .errors import DataError
from .types import (
    ACCEPT,
    DEGRADE,
    EDIT,
    REJECT,
    SIGNALS,
    PreferenceDataset,
    PreferencePair,
    Sample,
    SampleDataset,
)

#: The ordered feature names the QA axes map to (matches the §9.5 Critic axes plus
#: two optional perceptual extras). A model trained on these scores live records.
FEATURE_NAMES: tuple[str, ...] = (
    "ccs",
    "style",
    "timeline",
    "motion",
    "aesthetic",
    "temporal",
)


@dataclass(frozen=True)
class DirectorEvent:
    """One QA verdict + director disposition for a rendered shot (§9.5).

    Raw axes use the §9.5 sign conventions: ``ccs`` ∈ [0,1] (higher better),
    ``style_drift`` ≥ 0 (lower better), ``timeline_ok`` boolean, ``motion_artifact``
    ∈ [0,1] (lower better). ``aesthetic`` / ``temporal`` are optional 0..1 goodness
    scores. ``disposition`` is one of the director signals; ``edit_magnitude`` sizes
    an ``edit`` (0 = trivial tweak, 1 = wholesale rework).
    """

    ccs: float
    style_drift: float
    timeline_ok: bool
    motion_artifact: float
    disposition: str
    aesthetic: float = 1.0
    temporal: float = 1.0
    edit_magnitude: float = 0.0
    shot_id: str | None = None
    beat_id: str | None = None
    book_id: str | None = None

    def __post_init__(self) -> None:
        if self.disposition not in SIGNALS:
            raise DataError(
                f"disposition must be one of {SIGNALS}, got {self.disposition!r}"
            )
        for name, v in (
            ("ccs", self.ccs),
            ("style_drift", self.style_drift),
            ("motion_artifact", self.motion_artifact),
            ("aesthetic", self.aesthetic),
            ("temporal", self.temporal),
            ("edit_magnitude", self.edit_magnitude),
        ):
            fv = float(v)
            if fv != fv or fv in (float("inf"), float("-inf")):  # NaN/inf guard
                raise DataError(f"DirectorEvent.{name} must be finite, got {v!r}")

    def features(self) -> tuple[float, ...]:
        """Normalize the raw axes into the 0..1 goodness feature space.

        ``1.0`` is always "ideal": CCS / aesthetic / temporal pass through;
        style-drift and motion-artifact are inverted (and clamped to [0,1]); the
        timeline boolean becomes 1.0 (no contradiction) or 0.0.
        """

        style_good = max(0.0, 1.0 - float(self.style_drift))
        motion_good = max(0.0, 1.0 - float(self.motion_artifact))
        return (
            min(1.0, max(0.0, float(self.ccs))),
            min(1.0, style_good),
            1.0 if self.timeline_ok else 0.0,
            min(1.0, motion_good),
            min(1.0, max(0.0, float(self.aesthetic))),
            min(1.0, max(0.0, float(self.temporal))),
        )


def sample_from_event(event: DirectorEvent) -> Sample:
    """Convert one :class:`DirectorEvent` into a labelled :class:`Sample`."""

    return Sample.from_signal(
        event.features(),
        event.disposition,
        edit_magnitude=event.edit_magnitude,
        shot_id=event.shot_id,
        book_id=event.book_id,
        source="episodic",
    )


def build_sample_dataset(
    events: Iterable[DirectorEvent], *, name: str = "director-signals"
) -> SampleDataset:
    """Convert a stream of director events into a :class:`SampleDataset`."""

    samples = [sample_from_event(e) for e in events]
    if not samples:
        raise DataError("build_sample_dataset received no events")
    return SampleDataset(samples=tuple(samples), name=name)


#: A disposition is "positive" (the director kept the shot) or "negative".
_POSITIVE = frozenset({ACCEPT})
_NEGATIVE = frozenset({REJECT, DEGRADE, EDIT})


def pairs_from_events(
    events: Sequence[DirectorEvent],
    *,
    require_same_beat: bool = True,
    min_strength: float = 0.2,
    name: str = "director-preferences",
) -> PreferenceDataset:
    """Derive preference pairs by contrasting accepted vs rejected shots.

    For each (accepted, negative) shot pair sharing a beat (when
    ``require_same_beat``), emit ``accepted ≻ negative``. The pair ``strength``
    scales with how decisive the contrast is (a hard reject contrasts more than a
    light edit), floored at ``min_strength`` and capped at 1. Pairs whose feature
    vectors happen to be identical are skipped (no learnable contrast). Raises if
    no admissible pair exists.
    """

    positives = [e for e in events if e.disposition in _POSITIVE]
    negatives = [e for e in events if e.disposition in _NEGATIVE]
    pairs: list[PreferencePair] = []
    for pos in positives:
        for neg in negatives:
            if require_same_beat and pos.beat_id != neg.beat_id:
                continue
            if require_same_beat and pos.beat_id is None:
                continue
            wf = pos.features()
            lf = neg.features()
            if wf == lf:
                continue
            # An edit is a soft negative (less decisive, scaled down further by how
            # small the edit was); a degrade/reject is a hard, fully-decisive one.
            if neg.disposition == EDIT:
                decisiveness = 0.5 * (1.0 - 0.5 * float(neg.edit_magnitude))
            else:
                decisiveness = 1.0
            strength = min(1.0, max(min_strength, decisiveness))
            pairs.append(
                PreferencePair(
                    winner=wf,
                    loser=lf,
                    strength=strength,
                    book_id=pos.book_id,
                    source="episodic",
                )
            )
    if not pairs:
        raise DataError("no admissible accepted-vs-rejected pairs in the events")
    return PreferenceDataset(pairs=tuple(pairs), name=name)
