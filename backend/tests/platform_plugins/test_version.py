"""SemVer + range-matching unit tests."""

from __future__ import annotations

import pytest

from app.platform.plugins.errors import PluginValidationError
from app.platform.plugins.version import Version, VersionRange


def test_parse_and_str_roundtrip() -> None:
    v = Version.parse("1.2.3")
    assert (v.major, v.minor, v.patch) == (1, 2, 3)
    assert str(v) == "1.2.3"
    assert str(Version.parse("1.0.0-rc.1")) == "1.0.0-rc.1"


@pytest.mark.parametrize("bad", ["", "1", "1.2", "1.2.3.4", "v1.2.3", "1.2.x", "01.2.3"])
def test_invalid_version_rejected(bad: str) -> None:
    with pytest.raises(PluginValidationError):
        Version.parse(bad)


def test_ordering_core() -> None:
    assert Version.parse("1.0.0") < Version.parse("1.0.1")
    assert Version.parse("1.2.0") < Version.parse("2.0.0")
    assert Version.parse("1.0.0") == Version.parse("1.0.0")


def test_prerelease_lower_than_release() -> None:
    assert Version.parse("1.0.0-rc.1") < Version.parse("1.0.0")
    assert Version.parse("1.0.0-alpha") < Version.parse("1.0.0-beta")
    assert Version.parse("1.0.0-alpha.1") < Version.parse("1.0.0-alpha.2")
    assert Version.parse("1.0.0-alpha") < Version.parse("1.0.0-alpha.1")


def test_sorted_versions() -> None:
    vs = [Version.parse(s) for s in ("1.2.0", "1.0.0", "2.0.0-rc.1", "1.10.0", "1.2.0-beta")]
    ordered = [str(v) for v in sorted(vs)]
    assert ordered == ["1.0.0", "1.2.0-beta", "1.2.0", "1.10.0", "2.0.0-rc.1"]


def test_range_star_matches_everything() -> None:
    rng = VersionRange.parse("*")
    assert rng.matches("0.0.1")
    assert rng.matches("99.0.0")


def test_range_comparators() -> None:
    rng = VersionRange.parse(">=1.2.0,<2.0.0")
    assert rng.matches("1.2.0")
    assert rng.matches("1.9.9")
    assert not rng.matches("2.0.0")
    assert not rng.matches("1.1.9")


def test_caret_range() -> None:
    rng = VersionRange.parse("^1.2.3")
    assert rng.matches("1.2.3")
    assert rng.matches("1.9.0")
    assert not rng.matches("2.0.0")
    assert not rng.matches("1.2.2")


def test_caret_zero_major_pins_minor() -> None:
    rng = VersionRange.parse("^0.2.3")
    assert rng.matches("0.2.9")
    assert not rng.matches("0.3.0")


def test_tilde_range() -> None:
    rng = VersionRange.parse("~1.2.3")
    assert rng.matches("1.2.9")
    assert not rng.matches("1.3.0")


def test_xrange() -> None:
    rng = VersionRange.parse("1.2.x")
    assert rng.matches("1.2.0")
    assert rng.matches("1.2.99")
    assert not rng.matches("1.3.0")
    major_only = VersionRange.parse("1.x")
    assert major_only.matches("1.9.9")
    assert not major_only.matches("2.0.0")


def test_exact_version() -> None:
    rng = VersionRange.parse("==1.4.1")
    assert rng.matches("1.4.1")
    assert not rng.matches("1.4.2")


def test_prerelease_excluded_from_stable_range() -> None:
    # A stable caret range should NOT match an unrelated prerelease.
    assert not VersionRange.parse("^1.0.0").matches("2.0.0-rc.1")
    # But a range that explicitly names the prerelease core admits it.
    assert VersionRange.parse(">=1.0.0-rc.1,<2.0.0").matches("1.0.0-rc.1")
