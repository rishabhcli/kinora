"""SemVer + VersionRange: parsing, ordering, bumping, ranges, intersection."""

from __future__ import annotations

import pytest

from app.servicemesh.errors import VersionRangeError
from app.servicemesh.versioning import SemVer, VersionRange


def test_parse_roundtrips() -> None:
    assert str(SemVer.parse("1.2.3")) == "1.2.3"
    assert str(SemVer.parse("0.0.1")) == "0.0.1"
    assert str(SemVer.parse("2.0.0-rc.1")) == "2.0.0-rc.1"
    assert str(SemVer.parse("1.0.0+build.5")) == "1.0.0+build.5"


@pytest.mark.parametrize("bad", ["", "1", "1.2", "v1.2.3", "1.2.x", "01.2.3"])
def test_parse_rejects_garbage(bad: str) -> None:
    with pytest.raises(VersionRangeError):
        SemVer.parse(bad)


def test_ordering_core() -> None:
    assert SemVer.parse("1.0.0") < SemVer.parse("1.0.1")
    assert SemVer.parse("1.0.0") < SemVer.parse("1.1.0")
    assert SemVer.parse("1.9.9") < SemVer.parse("2.0.0")
    assert SemVer.parse("2.0.0") > SemVer.parse("1.99.99")


def test_prerelease_sorts_below_release() -> None:
    assert SemVer.parse("1.0.0-rc.1") < SemVer.parse("1.0.0")
    assert SemVer.parse("1.0.0-alpha") < SemVer.parse("1.0.0-beta")
    assert SemVer.parse("1.0.0-alpha.1") < SemVer.parse("1.0.0-alpha.2")


def test_build_metadata_ignored_for_equality() -> None:
    assert SemVer.parse("1.2.3+a") == SemVer.parse("1.2.3+b")
    assert hash(SemVer.parse("1.2.3+a")) == hash(SemVer.parse("1.2.3+b"))


def test_is_stable() -> None:
    assert SemVer.parse("1.0.0").is_stable
    assert SemVer.parse("2.5.1").is_stable
    assert not SemVer.parse("0.9.9").is_stable
    assert not SemVer.parse("1.0.0-rc.1").is_stable


def test_bump() -> None:
    v = SemVer.parse("1.4.2")
    assert v.bump("major") == SemVer.parse("2.0.0")
    assert v.bump("minor") == SemVer.parse("1.5.0")
    assert v.bump("patch") == SemVer.parse("1.4.3")
    with pytest.raises(VersionRangeError):
        v.bump("nope")


def test_coerce_accepts_both() -> None:
    assert SemVer.coerce("1.0.0") == SemVer.coerce(SemVer.parse("1.0.0"))


def test_same_major() -> None:
    assert SemVer.parse("1.0.0").same_major(SemVer.parse("1.9.0"))
    assert not SemVer.parse("1.0.0").same_major(SemVer.parse("2.0.0"))


# -- VersionRange ----------------------------------------------------------- #
def test_range_parse_and_contains() -> None:
    r = VersionRange.parse(">=1.2.0,<2.0.0")
    assert r.contains(SemVer.parse("1.2.0"))
    assert r.contains(SemVer.parse("1.9.9"))
    assert not r.contains(SemVer.parse("2.0.0"))
    assert not r.contains(SemVer.parse("1.1.9"))


def test_range_pin() -> None:
    r = VersionRange.parse("==1.4.0")
    assert r.contains(SemVer.parse("1.4.0"))
    assert not r.contains(SemVer.parse("1.4.1"))
    assert not r.contains(SemVer.parse("1.3.9"))


def test_range_unbounded_above() -> None:
    r = VersionRange.parse(">=1.0.0")
    assert r.contains(SemVer.parse("99.0.0"))
    assert not r.contains(SemVer.parse("0.9.0"))


def test_range_intersect_overlap() -> None:
    a = VersionRange.parse(">=1.0.0,<3.0.0")
    b = VersionRange.parse(">=2.0.0,<4.0.0")
    overlap = a.intersect(b)
    assert overlap is not None
    assert overlap.min_inclusive == SemVer.parse("2.0.0")
    assert overlap.max_exclusive == SemVer.parse("3.0.0")


def test_range_intersect_disjoint() -> None:
    a = VersionRange.parse(">=1.0.0,<2.0.0")
    b = VersionRange.parse(">=3.0.0,<4.0.0")
    assert a.intersect(b) is None


def test_range_rejects_empty_and_malformed() -> None:
    with pytest.raises(VersionRangeError):
        VersionRange.parse("")
    with pytest.raises(VersionRangeError):
        VersionRange.parse(">=2.0.0,<1.0.0")  # empty interval
    with pytest.raises(VersionRangeError):
        VersionRange.parse("<2.0.0")  # no lower bound
    with pytest.raises(VersionRangeError):
        VersionRange.parse("~1.0.0")  # unsupported operator
