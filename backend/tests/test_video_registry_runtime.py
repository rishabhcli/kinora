"""Runtime registry + weighted-picker tests (capability queries, canary, hot-reload)."""

from __future__ import annotations

import pytest

from app.video.registry.capabilities import CapabilityProfile, VideoMode
from app.video.registry.catalog import (
    CatalogError,
    ProviderEntry,
    ProviderKind,
    RolloutState,
    load_catalog_text,
)
from app.video.registry.picker import (
    WeightedCandidate,
    expected_distribution,
    pick_weighted,
)
from app.video.registry.registry import VideoProviderRegistry, register_runtime

# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #

_CATALOG_YAML = """
version: 1
providers:
  - id: incumbent
    kind: frontier
    weight: 95
    rollout: ga
    cost_tier: turbo
    capabilities:
      modes: [t2v, i2v, r2v]
      resolutions: [480P, 720P, 1080P]
      min_duration_s: 3
      max_duration_s: 10
  - id: canary
    kind: frontier
    weight: 5
    rollout: canary
    cost_tier: quality
    capabilities:
      modes: [i2v, r2v]
      resolutions: [720P, 1080P]
      min_duration_s: 3
      max_duration_s: 10
      supports_audio: true
  - id: cheap-short
    kind: frontier
    weight: 10
    rollout: ga
    cost_tier: cheap
    capabilities:
      modes: [t2v]
      resolutions: [720P]
      min_duration_s: 6
      max_duration_s: 6
  - id: off-model
    kind: open
    enabled: false
    weight: 50
    capabilities:
      modes: [t2v]
      resolutions: [720P]
"""


def _registry() -> VideoProviderRegistry:
    return VideoProviderRegistry(load_catalog_text(_CATALOG_YAML))


# --------------------------------------------------------------------------- #
# Lookup + register/unregister
# --------------------------------------------------------------------------- #


def test_lookup_and_membership() -> None:
    reg = _registry()
    assert len(reg) == 4
    assert "incumbent" in reg
    assert reg.get("nope") is None
    assert reg.require("incumbent").id == "incumbent"
    with pytest.raises(KeyError):
        reg.require("nope")


def test_register_and_unregister() -> None:
    reg = _registry()
    entry = ProviderEntry(
        id="experiment",
        kind=ProviderKind.OPEN,
        capabilities=CapabilityProfile(modes=["t2v"], resolutions=["720P"]),
    )
    reg.register(entry)
    assert "experiment" in reg
    # Duplicate without replace raises.
    with pytest.raises(ValueError, match="already registered"):
        reg.register(entry)
    # replace=True overwrites.
    reg.register(entry, replace=True)
    removed = reg.unregister("experiment")
    assert removed is not None and removed.id == "experiment"
    assert reg.unregister("experiment") is None  # idempotent


def test_register_runtime_batch() -> None:
    reg = _registry()
    extras = [
        ProviderEntry(
            id=f"x{i}",
            kind=ProviderKind.OPEN,
            capabilities=CapabilityProfile(modes=["t2v"], resolutions=["720P"]),
        )
        for i in range(3)
    ]
    register_runtime(reg, extras)
    assert all(f"x{i}" in reg for i in range(3))


# --------------------------------------------------------------------------- #
# Feature flags + weight overrides
# --------------------------------------------------------------------------- #


def test_enabled_override_toggles_routability() -> None:
    reg = _registry()
    assert reg.is_enabled("off-model") is False
    reg.set_enabled("off-model", True)
    assert reg.is_enabled("off-model") is True
    assert reg.effective_weight("off-model") == 50  # now routable
    reg.clear_enabled_override("off-model")
    assert reg.is_enabled("off-model") is False
    with pytest.raises(KeyError):
        reg.set_enabled("ghost", True)


def test_weight_override() -> None:
    reg = _registry()
    assert reg.effective_weight("canary") == 5
    reg.set_weight("canary", 25)
    assert reg.effective_weight("canary") == 25
    reg.clear_weight_override("canary")
    assert reg.effective_weight("canary") == 5
    with pytest.raises(ValueError, match=">= 0"):
        reg.set_weight("canary", -1)


def test_disabled_provider_has_zero_effective_weight() -> None:
    reg = _registry()
    # off-model is enabled=False => weight 0 even though catalog weight is 50.
    assert reg.effective_weight("off-model") == 0.0
    assert "off-model" not in [e.id for e in reg.routable()]


# --------------------------------------------------------------------------- #
# Capability queries
# --------------------------------------------------------------------------- #


def test_query_by_mode_resolution_duration() -> None:
    reg = _registry()
    # r2v at >=720P for >=8s: incumbent (3..10) and canary (3..10) qualify;
    # cheap-short is t2v-only; off-model disabled.
    matches = reg.query(mode="r2v", resolution="720P", duration_s=8)
    ids = [e.id for e in matches]
    assert ids == ["incumbent", "canary"]  # best-weighted first


def test_query_long_form_mode_alias() -> None:
    reg = _registry()
    short = reg.query(mode=VideoMode.R2V)
    long = reg.query(mode="reference_to_video")
    assert [e.id for e in short] == [e.id for e in long]


def test_query_duration_window_excludes_out_of_range() -> None:
    reg = _registry()
    # 6s t2v: incumbent (3..10) and cheap-short (6..6) qualify.
    matches = reg.query(mode="t2v", duration_s=6)
    assert {e.id for e in matches} == {"incumbent", "cheap-short"}
    # 8s t2v: cheap-short only does exactly 6s, so it drops out.
    matches = reg.query(mode="t2v", duration_s=8)
    assert {e.id for e in matches} == {"incumbent"}


def test_query_require_audio_and_kind_filters() -> None:
    reg = _registry()
    audio = reg.query(require_audio=True)
    assert [e.id for e in audio] == ["canary"]  # only canary advertises audio
    opens = reg.query(kind=ProviderKind.OPEN)
    assert opens == []  # the only OPEN model is disabled => not routable
    opens_incl = reg.query(kind=ProviderKind.OPEN, include_disabled=True)
    assert [e.id for e in opens_incl] == ["off-model"]


def test_query_excludes_resolution_beyond_reach() -> None:
    reg = _registry()
    # cheap-short tops out at 720P; a 1080P floor excludes it.
    matches = reg.query(mode="t2v", resolution="1080P")
    assert {e.id for e in matches} == {"incumbent"}


# --------------------------------------------------------------------------- #
# Weighted picker (deterministic + statistical canary distribution)
# --------------------------------------------------------------------------- #


def test_pick_weighted_is_deterministic() -> None:
    candidates = [WeightedCandidate("a", 50), WeightedCandidate("b", 50)]
    first = pick_weighted(candidates, "shot-42")
    again = pick_weighted(candidates, "shot-42")
    assert first == again
    assert first in {"a", "b"}


def test_pick_weighted_empty_or_all_zero_returns_none() -> None:
    assert pick_weighted([], "k") is None
    assert pick_weighted([WeightedCandidate("a", 0)], "k") is None


def test_expected_distribution_normalizes() -> None:
    dist = expected_distribution([WeightedCandidate("a", 30), WeightedCandidate("b", 10)])
    assert dist["a"] == pytest.approx(0.75)
    assert dist["b"] == pytest.approx(0.25)
    assert expected_distribution([]) == {}


def test_canary_distribution_is_statistically_close() -> None:
    # 95/5 split — sweep many stable keys and confirm the empirical share lands
    # near the configured 5% canary slice. Deterministic per key, so this test
    # has no RNG and never flakes across runs.
    candidates = [WeightedCandidate("incumbent", 95), WeightedCandidate("canary", 5)]
    n = 20_000
    counts = {"incumbent": 0, "canary": 0}
    for i in range(n):
        chosen = pick_weighted(candidates, f"shot-{i}")
        assert chosen is not None
        counts[chosen] += 1
    canary_share = counts["canary"] / n
    assert canary_share == pytest.approx(0.05, abs=0.01)  # 5% ± 1pp


def test_registry_pick_filters_then_weights() -> None:
    reg = _registry()
    # r2v restricts to incumbent + canary; pick must be one of them, stably.
    chosen = reg.pick("shot-7", mode="r2v", resolution="720P")
    assert chosen is not None and chosen.id in {"incumbent", "canary"}
    again = reg.pick("shot-7", mode="r2v", resolution="720P")
    assert again is not None and again.id == chosen.id  # stable
    # No provider can serve a 2160P r2v => None.
    assert reg.pick("shot-7", mode="r2v", resolution="2160P") is None


def test_registry_expected_split_matches_query() -> None:
    reg = _registry()
    split = reg.expected_split(mode="r2v")
    assert set(split) == {"incumbent", "canary"}
    assert split["incumbent"] == pytest.approx(95 / 100)
    assert split["canary"] == pytest.approx(5 / 100)


def test_weight_override_shifts_the_split() -> None:
    reg = _registry()
    reg.set_weight("canary", 95)  # 50/50 now
    split = reg.expected_split(mode="r2v")
    assert split["incumbent"] == pytest.approx(0.5)
    assert split["canary"] == pytest.approx(0.5)


# --------------------------------------------------------------------------- #
# Hot reload
# --------------------------------------------------------------------------- #


def test_reload_swaps_catalog_and_preserves_live_overrides() -> None:
    reg = _registry()
    reg.set_weight("incumbent", 1)  # a live override
    new_yaml = """
version: 2
providers:
  - id: incumbent
    kind: frontier
    weight: 80
    capabilities: {modes: [t2v], resolutions: [720P]}
  - id: newcomer
    kind: open
    weight: 20
    capabilities: {modes: [t2v], resolutions: [720P]}
"""
    reg.reload(text=new_yaml)
    assert "canary" not in reg  # gone in the new catalog
    assert "newcomer" in reg
    # The override for the still-present incumbent is kept.
    assert reg.effective_weight("incumbent") == 1


def test_reload_drops_overrides_for_vanished_ids() -> None:
    reg = _registry()
    reg.set_weight("canary", 40)
    reg.reload(
        text="version: 1\nproviders:\n  - id: incumbent\n    kind: frontier\n"
        "    capabilities: {modes: [t2v], resolutions: [720P]}\n"
    )
    # canary vanished; re-adding it via a later reload must not resurrect the override.
    snap = reg.snapshot()
    assert "canary" not in snap["weight_overrides"]


def test_reload_validate_before_swap_leaves_registry_untouched() -> None:
    reg = _registry()
    before = reg.ids()
    with pytest.raises(CatalogError):
        reg.reload(text="version: 1\nproviders:\n  - id: dup\n    kind: boguskind\n")
    assert reg.ids() == before  # nothing changed


def test_reload_rejects_both_text_and_path() -> None:
    reg = _registry()
    with pytest.raises(ValueError, match="at most one"):
        reg.reload(text="x", path="y")


def test_reload_no_args_loads_default_catalog() -> None:
    reg = _registry()
    reg.reload()  # checked-in default
    assert "wan2.1-t2v-turbo" in reg


def test_snapshot_reports_overrides_and_routable() -> None:
    reg = _registry()
    reg.set_enabled("off-model", True)
    reg.set_weight("canary", 12)
    snap = reg.snapshot()
    assert snap["enabled_overrides"] == {"off-model": True}
    assert snap["weight_overrides"] == {"canary": 12}
    assert "off-model" in snap["routable"]


def test_rollout_disabled_never_routable_even_when_enabled() -> None:
    reg = _registry()
    entry = ProviderEntry(
        id="killed",
        kind=ProviderKind.GATEWAY,
        weight=99,
        rollout=RolloutState.DISABLED,
        capabilities=CapabilityProfile(modes=["t2v"], resolutions=["720P"]),
    )
    reg.register(entry)
    reg.set_enabled("killed", True)
    assert reg.effective_weight("killed") == 0.0
    assert "killed" not in [e.id for e in reg.routable()]
