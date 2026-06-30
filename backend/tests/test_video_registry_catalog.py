"""Catalog parsing/validation + capability-profile vocabulary tests (infra-free)."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from app.video.registry.capabilities import CapabilityProfile, Resolution, VideoMode
from app.video.registry.catalog import (
    CatalogError,
    ProviderEntry,
    ProviderKind,
    RolloutState,
    default_catalog_path,
    dump_catalog_json,
    load_catalog_text,
    load_default_catalog,
)

# --------------------------------------------------------------------------- #
# Capability vocabulary
# --------------------------------------------------------------------------- #


def test_video_mode_coerce_short_long_and_synonym() -> None:
    assert VideoMode.coerce("t2v") is VideoMode.T2V
    assert VideoMode.coerce("text_to_video") is VideoMode.T2V  # WanMode spelling
    assert VideoMode.coerce("reference_to_video") is VideoMode.R2V
    assert VideoMode.coerce("EXTEND") is VideoMode.CONTINUATION  # synonym, case-insensitive
    assert VideoMode.coerce(VideoMode.I2V) is VideoMode.I2V


def test_video_mode_coerce_unknown_raises_with_accepted_list() -> None:
    with pytest.raises(ValueError, match="unknown video mode"):
        VideoMode.coerce("hologram")


def test_resolution_ordering_and_coerce() -> None:
    assert Resolution.HD_720.height == 720
    assert Resolution.FHD_1080.height > Resolution.HD_720.height
    assert Resolution.coerce("720p") is Resolution.HD_720
    assert Resolution.coerce(" 1080 ") is Resolution.FHD_1080
    with pytest.raises(ValueError, match="unknown resolution"):
        Resolution.coerce("8K")


def test_capability_profile_coerces_lists_and_computes_max_resolution() -> None:
    profile = CapabilityProfile(
        modes=["t2v", "image_to_video"],  # mixed short/long spelling
        resolutions=["480P", "720P", "1080P"],
        min_duration_s=3,
        max_duration_s=8,
    )
    assert profile.modes == {VideoMode.T2V, VideoMode.I2V}
    assert profile.max_resolution is Resolution.FHD_1080


def test_capability_profile_rejects_inverted_duration_window() -> None:
    with pytest.raises(ValueError, match="must not exceed"):
        CapabilityProfile(modes=["t2v"], resolutions=["720P"], min_duration_s=8, max_duration_s=5)


def test_capability_profile_satisfies_predicate() -> None:
    profile = CapabilityProfile(
        modes=["r2v", "i2v"],
        resolutions=["720P", "1080P"],
        min_duration_s=3,
        max_duration_s=10,
        supports_audio=False,
    )
    # Empty query => always serves.
    assert profile.satisfies() is True
    # Mode match + a minimum-resolution comparison + a duration in-window.
    assert profile.satisfies(mode="r2v", resolution="720P", duration_s=8) is True
    # Wrong mode.
    assert profile.satisfies(mode="t2v") is False
    # Resolution beyond reach.
    assert profile.satisfies(resolution="2160P") is False
    # Duration outside the window.
    assert profile.satisfies(duration_s=20) is False
    # Audio required but not advertised.
    assert profile.satisfies(require_audio=True) is False


def test_capability_profile_is_frozen() -> None:
    profile = CapabilityProfile(modes=["t2v"], resolutions=["720P"])
    with pytest.raises(ValidationError):  # pydantic frozen => ValidationError on set
        profile.min_duration_s = 99  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# Catalog parsing / validation
# --------------------------------------------------------------------------- #

_MINIMAL_YAML = """
version: 1
providers:
  - id: model-a
    kind: frontier
    cost_tier: turbo
    weight: 70
    capabilities:
      modes: [t2v]
      resolutions: [720P]
      min_duration_s: 3
      max_duration_s: 5
  - id: model-b
    kind: open
    enabled: false
    rollout: preview
    capabilities:
      modes: [i2v, r2v]
      resolutions: [480P, 720P]
"""


def test_load_catalog_text_yaml() -> None:
    catalog = load_catalog_text(_MINIMAL_YAML)
    assert catalog.version == 1
    assert catalog.ids() == ("model-a", "model-b")
    a = catalog.by_id("model-a")
    assert a is not None and a.kind is ProviderKind.FRONTIER and a.weight == 70
    b = catalog.by_id("model-b")
    assert b is not None and b.rollout is RolloutState.PREVIEW and b.enabled is False


def test_load_catalog_text_accepts_json() -> None:
    payload = {
        "version": 1,
        "providers": [
            {
                "id": "json-model",
                "kind": "gateway",
                "capabilities": {"modes": ["t2v"], "resolutions": ["720P"]},
            }
        ],
    }
    catalog = load_catalog_text(json.dumps(payload))
    assert catalog.ids() == ("json-model",)


def test_duplicate_ids_rejected() -> None:
    dupe = """
version: 1
providers:
  - id: dup
    kind: frontier
    capabilities: {modes: [t2v], resolutions: [720P]}
  - id: dup
    kind: open
    capabilities: {modes: [i2v], resolutions: [480P]}
"""
    with pytest.raises(CatalogError, match="duplicate provider id"):
        load_catalog_text(dupe)


def test_empty_catalog_rejected() -> None:
    with pytest.raises(CatalogError, match="empty"):
        load_catalog_text("")
    with pytest.raises(CatalogError):  # zero providers fails min_length
        load_catalog_text("version: 1\nproviders: []\n")


def test_bad_yaml_rejected() -> None:
    with pytest.raises(CatalogError, match="not valid YAML"):
        load_catalog_text("providers: [unbalanced")


def test_unknown_field_rejected() -> None:
    bad = """
version: 1
providers:
  - id: x
    kind: frontier
    bogus_field: 1
    capabilities: {modes: [t2v], resolutions: [720P]}
"""
    with pytest.raises(CatalogError):
        load_catalog_text(bad)


def test_non_mapping_root_rejected() -> None:
    with pytest.raises(CatalogError, match="must be a mapping"):
        load_catalog_text("- just\n- a\n- list\n")


def test_provider_entry_label_and_routable_flags() -> None:
    on = ProviderEntry(
        id="on",
        kind=ProviderKind.FRONTIER,
        capabilities=CapabilityProfile(modes=["t2v"], resolutions=["720P"]),
    )
    assert on.label == "on"  # falls back to id
    assert on.is_routable is True

    off = ProviderEntry(
        id="off",
        kind=ProviderKind.FRONTIER,
        display_name="Off Model",
        enabled=False,
        capabilities=CapabilityProfile(modes=["t2v"], resolutions=["720P"]),
    )
    assert off.label == "Off Model"
    assert off.is_routable is False

    zero = ProviderEntry(
        id="zero",
        kind=ProviderKind.FRONTIER,
        weight=0.0,
        capabilities=CapabilityProfile(modes=["t2v"], resolutions=["720P"]),
    )
    assert zero.is_routable is False  # zero weight => not routable

    disabled = ProviderEntry(
        id="dis",
        kind=ProviderKind.FRONTIER,
        rollout=RolloutState.DISABLED,
        capabilities=CapabilityProfile(modes=["t2v"], resolutions=["720P"]),
    )
    assert disabled.is_routable is False


# --------------------------------------------------------------------------- #
# Shipped default catalog
# --------------------------------------------------------------------------- #


def test_default_catalog_file_exists_and_parses() -> None:
    assert default_catalog_path().exists()
    catalog = load_default_catalog()
    assert len(catalog.providers) >= 3
    # The documented Wan defaults are present.
    assert catalog.by_id("wan2.1-t2v-turbo") is not None
    assert catalog.by_id("wan2.1-i2v-turbo") is not None


def test_default_catalog_round_trips_through_json() -> None:
    catalog = load_default_catalog()
    text = dump_catalog_json(catalog)
    reparsed = load_catalog_text(text, source="<roundtrip>")
    assert reparsed.ids() == catalog.ids()
