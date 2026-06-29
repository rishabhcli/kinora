"""Tests for the director-event → reward/preference ingestion seam."""

from __future__ import annotations

import pytest

from app.mlplatform.alignment.errors import DataError
from app.mlplatform.alignment.reward_model import RewardModelTrainer
from app.mlplatform.alignment.signals import (
    DirectorEvent,
    build_sample_dataset,
    pairs_from_events,
    sample_from_event,
)


def _accept(beat: str = "b1") -> DirectorEvent:
    return DirectorEvent(
        ccs=0.95,
        style_drift=0.02,
        timeline_ok=True,
        motion_artifact=0.05,
        disposition="accept",
        beat_id=beat,
    )


def _reject(beat: str = "b1") -> DirectorEvent:
    return DirectorEvent(
        ccs=0.6,
        style_drift=0.2,
        timeline_ok=True,
        motion_artifact=0.4,
        disposition="reject",
        beat_id=beat,
    )


def test_event_feature_normalization() -> None:
    e = DirectorEvent(
        ccs=0.9,
        style_drift=0.1,  # -> 0.9 goodness
        timeline_ok=False,  # -> 0.0
        motion_artifact=0.25,  # -> 0.75 goodness
        disposition="accept",
        aesthetic=0.8,
        temporal=0.7,
    )
    feats = e.features()
    assert feats == pytest.approx((0.9, 0.9, 0.0, 0.75, 0.8, 0.7))
    # All features land in [0, 1].
    assert all(0.0 <= f <= 1.0 for f in feats)


def test_event_validation() -> None:
    with pytest.raises(DataError):
        DirectorEvent(
            ccs=0.9, style_drift=0.0, timeline_ok=True,
            motion_artifact=0.0, disposition="bogus",
        )
    with pytest.raises(DataError):
        DirectorEvent(
            ccs=float("nan"), style_drift=0.0, timeline_ok=True,
            motion_artifact=0.0, disposition="accept",
        )


def test_sample_from_event_labels() -> None:
    assert sample_from_event(_accept()).reward == pytest.approx(1.0)
    assert sample_from_event(_reject()).reward == pytest.approx(0.0)
    edit = DirectorEvent(
        ccs=0.85, style_drift=0.05, timeline_ok=True, motion_artifact=0.1,
        disposition="edit", edit_magnitude=0.8,
    )
    s = sample_from_event(edit)
    assert s.reward < 0.5  # a big edit is a soft-then-hard negative
    assert s.source == "episodic"


def test_build_sample_dataset_trains_a_model() -> None:
    events = [_accept() for _ in range(20)] + [_reject() for _ in range(20)]
    ds = build_sample_dataset(events)
    assert len(ds) == 40
    assert ds.dim == 6
    model = RewardModelTrainer(l2=0.01).fit(ds)
    # The accepted region scores well above the rejected one.
    assert model.reward(_accept().features()) > model.reward(_reject().features())


def test_build_sample_dataset_empty_raises() -> None:
    with pytest.raises(DataError):
        build_sample_dataset([])


def test_pairs_from_events_same_beat() -> None:
    events = [_accept(beat="b1"), _reject(beat="b1"), _reject(beat="b2")]
    pd = pairs_from_events(events)
    # Only the same-beat accepted/rejected contrast is admissible.
    assert len(pd) == 1
    assert pd.pairs[0].winner == _accept(beat="b1").features()
    assert pd.pairs[0].strength == pytest.approx(1.0)  # a hard reject


def test_pairs_edit_is_softer_than_reject() -> None:
    acc = _accept(beat="b1")
    edit = DirectorEvent(
        ccs=0.8, style_drift=0.1, timeline_ok=True, motion_artifact=0.2,
        disposition="edit", edit_magnitude=0.5, beat_id="b1",
    )
    pd = pairs_from_events([acc, edit])
    assert len(pd) == 1
    # An edit contrast is weaker than a hard reject (strength < 1).
    assert pd.pairs[0].strength < 1.0


def test_pairs_no_admissible_raises() -> None:
    # Two accepts, no negatives => no pairs.
    with pytest.raises(DataError):
        pairs_from_events([_accept(), _accept(beat="b2")])


def test_pairs_cross_beat_allowed_when_relaxed() -> None:
    events = [_accept(beat="b1"), _reject(beat="b2")]
    pd = pairs_from_events(events, require_same_beat=False)
    assert len(pd) == 1
