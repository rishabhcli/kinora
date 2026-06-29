"""Validation + adapter tests for the alignment type layer."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from app.mlplatform.alignment.errors import DataError
from app.mlplatform.alignment.types import (
    ACCEPT,
    EDIT,
    REJECT,
    PreferenceDataset,
    PreferencePair,
    Sample,
    SampleDataset,
    as_sample_dataset,
)


def test_sample_validates_and_normalizes() -> None:
    s = Sample(features=[0.9, 0.1], reward=1.0)
    assert s.features == (0.9, 0.1)
    assert s.dim == 2
    with pytest.raises(DataError):
        Sample(features=[], reward=1.0)
    with pytest.raises(DataError):
        Sample(features=[float("nan")], reward=1.0)
    with pytest.raises(DataError):
        Sample(features=[0.1], reward=1.5)  # reward out of [0,1]
    with pytest.raises(DataError):
        Sample(features=[0.1], reward=0.5, weight=-1.0)


def test_sample_from_signal_reward_mapping() -> None:
    feats = [0.9, 0.05]
    assert Sample.from_signal(feats, ACCEPT).reward == pytest.approx(1.0)
    assert Sample.from_signal(feats, REJECT).reward == pytest.approx(0.0)
    # An edit with magnitude 0 sits at the soft-negative default; a big edit
    # collapses toward reject and earns more weight.
    small = Sample.from_signal(feats, EDIT, edit_magnitude=0.0)
    big = Sample.from_signal(feats, EDIT, edit_magnitude=1.0)
    assert small.reward > big.reward
    assert big.reward == pytest.approx(0.0)
    assert big.weight > small.weight


def test_preference_pair_validation_and_diff() -> None:
    p = PreferencePair(winner=[1.0, 0.0], loser=[0.0, 1.0])
    assert p.diff() == (1.0, -1.0)
    with pytest.raises(DataError):
        PreferencePair(winner=[1.0], loser=[1.0, 0.0])  # dim mismatch
    with pytest.raises(DataError):
        PreferencePair(winner=[1.0, 0.0], loser=[1.0, 0.0])  # identical
    with pytest.raises(DataError):
        PreferencePair(winner=[1.0], loser=[0.0], strength=0.0)  # bad strength


def test_sample_dataset_enforces_uniform_dim() -> None:
    ds = SampleDataset(
        samples=(Sample([0.1, 0.2], 1.0), Sample([0.3, 0.4], 0.0))
    )
    assert ds.dim == 2
    assert len(ds) == 2
    assert ds.n_positive == 1 and ds.n_negative == 1
    with pytest.raises(DataError):
        SampleDataset(samples=(Sample([0.1], 1.0), Sample([0.1, 0.2], 0.0)))
    with pytest.raises(DataError):
        SampleDataset(samples=())


def test_preference_dataset_uniform_dim() -> None:
    pd = PreferenceDataset(
        pairs=(
            PreferencePair([1.0, 0.0], [0.0, 1.0]),
            PreferencePair([0.5, 0.5], [0.1, 0.1]),
        )
    )
    assert pd.dim == 2 and len(pd) == 2
    with pytest.raises(DataError):
        PreferenceDataset(pairs=())


def test_as_sample_dataset_passthrough_and_iterable() -> None:
    ds = SampleDataset(samples=(Sample([0.1, 0.2], 1.0),))
    assert as_sample_dataset(ds) is ds
    out = as_sample_dataset([Sample([0.1, 0.2], 1.0), Sample([0.3, 0.4], 0.0)])
    assert len(out) == 2


def test_as_sample_dataset_duck_types_facet_a_rows() -> None:
    # Simulate facet A's Dataset rows: objects exposing `features` + `reward`,
    # and rows exposing `features` + `signal`.
    @dataclass
    class RowR:
        features: tuple[float, ...]
        reward: float

    @dataclass
    class RowS:
        features: tuple[float, ...]
        signal: str
        edit_magnitude: float = 0.0

    class FakeDatasetA:
        def __init__(self, rows: list[object]) -> None:
            self._rows = rows

        def __iter__(self):  # type: ignore[no-untyped-def]
            return iter(self._rows)

        def __len__(self) -> int:
            return len(self._rows)

    fa = FakeDatasetA([RowR((0.9, 0.1), 1.0), RowS((0.2, 0.8), REJECT)])
    out = as_sample_dataset(fa)
    assert len(out) == 2
    assert out.samples[0].reward == pytest.approx(1.0)
    assert out.samples[1].reward == pytest.approx(0.0)


def test_as_sample_dataset_rejects_bad_rows() -> None:
    class NoFeatures:
        reward = 1.0

    with pytest.raises(DataError):
        as_sample_dataset([NoFeatures()])

    class NoLabel:
        features = (0.1, 0.2)

    with pytest.raises(DataError):
        as_sample_dataset([NoLabel()])
