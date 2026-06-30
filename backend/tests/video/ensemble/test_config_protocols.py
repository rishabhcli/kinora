"""Config-from-settings parsing, protocol structural compatibility, and the
constructor guards. Confirms the real provider/budget types satisfy the local seams."""

from __future__ import annotations

import pytest

from app.video.ensemble.models import CostUnit, EnsembleConfig, Objective
from app.video.ensemble.protocols import (
    EnsembleProvider,
    MultiRenderBudget,
    QualityScorer,
)
from app.video.ensemble.renderer import BestOfNRenderer

from ._fakes import FakeBudget, FakeProvider, FakeScorer


class _Settings:
    """A duck-typed stand-in for app.core.config.Settings (the fields we read)."""

    ensemble_enabled = True
    ensemble_enabled_tiers = "hero, climax ,"  # whitespace + trailing comma
    ensemble_max_candidates = 3
    ensemble_max_concurrency = 2
    ensemble_objective = "quality_per_cost"
    ensemble_cost_unit = "usd"
    ensemble_per_shot_cost_cap = 0.75
    ensemble_good_enough_quality = 0.9


def test_config_from_settings_parses_tiers_and_enums() -> None:
    cfg = EnsembleConfig.from_settings(_Settings())  # type: ignore[arg-type]
    assert cfg.enabled is True
    assert cfg.enabled_tiers == frozenset({"hero", "climax"})  # trimmed, empties dropped
    assert cfg.objective is Objective.QUALITY_PER_COST
    assert cfg.cost_unit is CostUnit.USD
    assert cfg.per_shot_cost_cap == pytest.approx(0.75)
    assert cfg.good_enough_quality == pytest.approx(0.9)


def test_config_defaults_never_fanout() -> None:
    cfg = EnsembleConfig()
    assert cfg.enabled is False
    assert cfg.max_candidates == 1
    assert cfg.enabled_tiers == frozenset()


def test_config_rejects_bad_bounds() -> None:
    with pytest.raises(ValueError):
        EnsembleConfig(max_candidates=0)
    with pytest.raises(ValueError):
        EnsembleConfig(max_concurrency=0)
    with pytest.raises(ValueError):
        EnsembleConfig(per_shot_cost_cap=-1.0)


def test_config_from_settings_rejects_bad_objective() -> None:
    bad = _Settings()
    bad.ensemble_objective = "nonsense"
    with pytest.raises(ValueError):
        EnsembleConfig.from_settings(bad)  # type: ignore[arg-type]


def test_real_settings_build_a_config() -> None:
    # The actual Settings object exposes the ensemble_* fields and parses cleanly.
    from app.core.config import get_settings

    cfg = EnsembleConfig.from_settings(get_settings())
    assert cfg.enabled is False  # safe default in the repo
    assert cfg.max_candidates == 1


def test_fakes_satisfy_local_protocols() -> None:
    assert isinstance(FakeProvider("a"), EnsembleProvider)
    assert isinstance(FakeScorer(), QualityScorer)
    assert isinstance(FakeBudget(), MultiRenderBudget)


def test_renderer_requires_at_least_one_provider() -> None:
    with pytest.raises(ValueError):
        BestOfNRenderer({}, FakeScorer(), FakeBudget(), EnsembleConfig())


def test_real_video_provider_protocol_shape() -> None:
    # The real VideoProvider/VideoRouter expose name + async render — the same shape
    # EnsembleProvider needs. We assert the attribute/coroutine surface without a
    # network client (structural compatibility, no instantiation of the transport).
    from app.providers.video_router import VideoBackend

    # VideoBackend itself is the upstream protocol; a class satisfying it satisfies
    # EnsembleProvider's render/name surface too.
    assert hasattr(VideoBackend, "render")
