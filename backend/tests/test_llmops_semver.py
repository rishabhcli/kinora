"""Unit tests for the prompt-registry semantic-version helpers (no infra)."""

from __future__ import annotations

import pytest

from app.llmops.errors import InvalidVersionError
from app.llmops.semver import SemVer, is_valid, latest, parse, sort_versions


def test_parse_core() -> None:
    v = SemVer.parse("3.4.5")
    assert (v.major, v.minor, v.patch) == (3, 4, 5)
    assert v.prerelease is None
    assert str(v) == "3.4.5"


def test_parse_prerelease_and_build() -> None:
    v = SemVer.parse("1.2.3-rc.1+build.7")
    assert v.prerelease == "rc.1"
    assert v.build == "build.7"
    assert v.is_prerelease
    assert str(v) == "1.2.3-rc.1+build.7"


@pytest.mark.parametrize("bad", ["", "1", "1.2", "1.2.x", "v1.2.3", "01.2.3", "1.2.3.4"])
def test_parse_rejects_malformed(bad: str) -> None:
    assert not is_valid(bad)
    with pytest.raises(InvalidVersionError):
        parse(bad)


def test_from_prompt_tag() -> None:
    assert SemVer.from_prompt_tag("cinematographer@v3") == SemVer(3, 0, 0)
    assert SemVer.from_prompt_tag("adapter@v1") == SemVer(1, 0, 0)
    with pytest.raises(InvalidVersionError):
        SemVer.from_prompt_tag("adapter-v1")


def test_ordering_core() -> None:
    assert SemVer.parse("2.0.0") > SemVer.parse("1.9.9")
    assert SemVer.parse("1.2.0") > SemVer.parse("1.1.9")
    assert SemVer.parse("1.1.2") > SemVer.parse("1.1.1")


def test_prerelease_lower_than_release() -> None:
    # 1.0.0-rc1 < 1.0.0 (a pre-release has lower precedence, SemVer §11).
    assert SemVer.parse("1.0.0-rc.1") < SemVer.parse("1.0.0")
    assert SemVer.parse("1.0.0-alpha") < SemVer.parse("1.0.0-beta")
    assert SemVer.parse("1.0.0-alpha.1") < SemVer.parse("1.0.0-alpha.2")
    # numeric identifiers rank below alphanumeric
    assert SemVer.parse("1.0.0-1") < SemVer.parse("1.0.0-alpha")


def test_build_metadata_ignored_for_equality() -> None:
    assert SemVer.parse("1.0.0+a") == SemVer.parse("1.0.0+b")
    assert SemVer.parse("1.0.0+a") == SemVer.parse("1.0.0")


def test_bump() -> None:
    base = SemVer.parse("1.4.2")
    assert str(base.bump("major")) == "2.0.0"
    assert str(base.bump("minor")) == "1.5.0"
    assert str(base.bump("patch")) == "1.4.3"
    with pytest.raises(InvalidVersionError):
        base.bump("nonsense")


def test_bump_clears_prerelease() -> None:
    assert str(SemVer.parse("1.0.0-rc.1").bump("patch")) == "1.0.1"


def test_latest_and_sort() -> None:
    versions = ["1.0.0", "2.1.0", "2.0.5", "1.9.9"]
    assert latest(versions) == "2.1.0"
    assert sort_versions(versions) == ["1.0.0", "1.9.9", "2.0.5", "2.1.0"]
    assert sort_versions(versions, descending=True)[0] == "2.1.0"


def test_latest_empty_raises() -> None:
    with pytest.raises(ValueError):
        latest([])


def test_hashable() -> None:
    s = {SemVer.parse("1.0.0"), SemVer.parse("1.0.0"), SemVer.parse("2.0.0")}
    assert len(s) == 2
