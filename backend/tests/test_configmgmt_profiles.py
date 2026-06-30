"""Environment profiles: presets, overlay precedence, diffing (app.configmgmt.profiles).

Pure value transforms — no Settings mutation, no I/O.
"""

from __future__ import annotations

import pytest

from app.configmgmt.profiles import (
    PROFILES,
    Profile,
    ProfileName,
    diff_profiles,
    overlay,
    profile_for,
)

# --------------------------------------------------------------------------- #
# Names / aliases / presets
# --------------------------------------------------------------------------- #


def test_coerce_known_aliases() -> None:
    assert ProfileName.coerce("production") is ProfileName.PROD
    assert ProfileName.coerce("PROD") is ProfileName.PROD
    assert ProfileName.coerce("stage") is ProfileName.STAGING
    assert ProfileName.coerce("dev") is ProfileName.LOCAL
    assert ProfileName.coerce("ci") is ProfileName.TEST


def test_coerce_unknown_raises() -> None:
    with pytest.raises(ValueError, match="unknown environment profile"):
        ProfileName.coerce("qa-east-2")


def test_every_name_has_a_preset() -> None:
    for name in ProfileName:
        assert name in PROFILES
        assert profile_for(name).name is name


def test_presets_carry_no_secrets() -> None:
    # A profile must never bake in a credential — secrets come from the env.
    forbidden = {"dashscope_api_key", "jwt_secret", "api_key_pepper", "s3_secret_key"}
    for prof in PROFILES.values():
        assert forbidden.isdisjoint(prof.values)


def test_presets_keep_live_video_off() -> None:
    for prof in PROFILES.values():
        assert prof.values.get("kinora_live_video") is False


def test_profile_for_accepts_alias_string() -> None:
    assert profile_for("production").name is ProfileName.PROD


# --------------------------------------------------------------------------- #
# Overlay — last-wins precedence + provenance
# --------------------------------------------------------------------------- #


def test_overlay_last_wins() -> None:
    result = overlay({"a": 1, "b": 2}, {"b": 3, "c": 4})
    assert result.values == {"a": 1, "b": 3, "c": 4}


def test_overlay_records_provenance() -> None:
    result = overlay({"a": 1}, {"a": 2}, {"c": 9})
    assert result.source_of("a") == 1  # the second layer won
    assert result.source_of("c") == 2
    assert result.source_of("missing") is None


def test_overlay_none_is_a_real_override() -> None:
    result = overlay({"x": "set"}, {"x": None})
    assert result.values["x"] is None
    assert result.source_of("x") == 1


def test_overlay_accepts_profiles_and_mappings() -> None:
    result = overlay(profile_for("prod"), {"log_level": "WARNING"})
    assert result.values["log_level"] == "WARNING"  # env override wins
    assert result.values["app_env"] == "prod"  # from the preset
    assert result.source_of("log_level") == 1


def test_overlay_empty() -> None:
    assert overlay().values == {}


# --------------------------------------------------------------------------- #
# Diff — added / removed / changed
# --------------------------------------------------------------------------- #


def test_diff_added_removed_changed() -> None:
    left = Profile(ProfileName.STAGING, {"keep": 1, "drop": 2, "move": "a"})
    right = Profile(ProfileName.PROD, {"keep": 1, "move": "b", "new": 9})
    diff = diff_profiles(left, right)
    assert [c.field for c in diff.added] == ["new"]
    assert [c.field for c in diff.removed] == ["drop"]
    assert [c.field for c in diff.changed] == ["move"]
    assert not diff.is_empty


def test_diff_identical_is_empty() -> None:
    diff = diff_profiles({"a": 1, "b": 2}, {"a": 1, "b": 2})
    assert diff.is_empty
    assert diff.changes == ()


def test_diff_is_sorted_by_field() -> None:
    diff = diff_profiles({"z": 1, "a": 1}, {"z": 2, "a": 2})
    assert [c.field for c in diff.changed] == ["a", "z"]


def test_diff_only_restricts_fields() -> None:
    diff = diff_profiles({"a": 1, "b": 1, "c": 1}, {"a": 2, "b": 2, "c": 2}, only=["b"])
    assert [c.field for c in diff.changed] == ["b"]


def test_diff_change_kinds_and_dict() -> None:
    diff = diff_profiles({"gone": 1}, {"fresh": 2})
    kinds = {c.field: c.kind for c in diff.changes}
    assert kinds == {"gone": "removed", "fresh": "added"}
    d = diff.to_dict()
    assert d["added"][0]["field"] == "fresh"
    assert d["removed"][0]["field"] == "gone"
    # An added change carries no "left" key; a removed carries no "right".
    assert "left" not in d["added"][0]
    assert "right" not in d["removed"][0]


def test_staging_vs_prod_preset_diff_is_meaningful() -> None:
    # The two production-grade presets should differ at least on app_env.
    diff = diff_profiles(profile_for("staging"), profile_for("prod"))
    changed_fields = {c.field for c in diff.changed} | {c.field for c in diff.added}
    assert "app_env" in changed_fields
