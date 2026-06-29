"""A small, dependency-free SemVer 2.0.0 implementation + range matching.

Plugins are versioned and depend on each other (and on the host) by version
range, so the registry needs deterministic version comparison and constraint
satisfaction without pulling in a third-party semver package. This covers the
subset the platform uses:

* :class:`Version` — ``MAJOR.MINOR.PATCH`` with an optional ``-prerelease``,
  fully ordered per the SemVer precedence rules (numeric identifiers compare
  numerically; a prerelease is *lower* than its release).
* :class:`VersionRange` — a comma-separated conjunction of comparator terms
  (``>=1.2.0``, ``<2.0.0``, ``==1.4.1``, ``^1.2``, ``~1.2.3``, ``1.2.x``, ``*``).

Everything is pure and total: parsing a malformed string raises
:class:`~app.platform.plugins.errors.PluginValidationError`, never silently
coerces.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import total_ordering

from app.platform.plugins.errors import PluginValidationError

_CORE_RE = re.compile(
    r"^(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)"
    r"(?:-(?P<pre>[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)


@total_ordering
@dataclass(frozen=True, slots=True)
class Version:
    """A SemVer 2.0.0 version (build metadata is ignored for precedence)."""

    major: int
    minor: int
    patch: int
    prerelease: tuple[str, ...] = ()

    @classmethod
    def parse(cls, text: str) -> Version:
        """Parse ``MAJOR.MINOR.PATCH[-prerelease]`` (raises on malformed input)."""
        if not isinstance(text, str):
            raise PluginValidationError(f"version must be a string: {text!r}")
        m = _CORE_RE.match(text.strip())
        if m is None:
            raise PluginValidationError(f"invalid semantic version: {text!r}")
        pre = tuple(m.group("pre").split(".")) if m.group("pre") else ()
        return cls(
            major=int(m.group("major")),
            minor=int(m.group("minor")),
            patch=int(m.group("patch")),
            prerelease=pre,
        )

    @property
    def is_prerelease(self) -> bool:
        return bool(self.prerelease)

    @property
    def core(self) -> tuple[int, int, int]:
        return (self.major, self.minor, self.patch)

    def __str__(self) -> str:
        base = f"{self.major}.{self.minor}.{self.patch}"
        return f"{base}-{'.'.join(self.prerelease)}" if self.prerelease else base

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, Version):  # pragma: no cover - defensive
            return NotImplemented
        if self.core != other.core:
            return self.core < other.core
        # A version WITH a prerelease is lower than the same version WITHOUT one.
        if self.prerelease and not other.prerelease:
            return True
        if not self.prerelease and other.prerelease:
            return False
        return _pre_cmp(self.prerelease, other.prerelease) < 0


def _pre_cmp(a: tuple[str, ...], b: tuple[str, ...]) -> int:
    """Compare prerelease identifier tuples per SemVer precedence rules."""
    for x, y in zip(a, b, strict=False):
        xn, yn = x.isdigit(), y.isdigit()
        if xn and yn:
            ix, iy = int(x), int(y)
            if ix != iy:
                return -1 if ix < iy else 1
        elif xn != yn:
            # Numeric identifiers always have lower precedence than alphanumeric.
            return -1 if xn else 1
        elif x != y:
            return -1 if x < y else 1
    if len(a) != len(b):
        return -1 if len(a) < len(b) else 1
    return 0


# --------------------------------------------------------------------------- #
# Version ranges
# --------------------------------------------------------------------------- #

_OP_RE = re.compile(r"^(>=|<=|>|<|==|!=|\^|~)?\s*(.+)$")
_XRANGE_RE = re.compile(r"^(\d+|\*|x|X)(?:\.(\d+|\*|x|X))?(?:\.(\d+|\*|x|X))?$")


@dataclass(frozen=True, slots=True)
class _Comparator:
    op: str
    version: Version

    def matches(self, v: Version) -> bool:
        if self.op == ">=":
            return v >= self.version
        if self.op == "<=":
            return v <= self.version
        if self.op == ">":
            return v > self.version
        if self.op == "<":
            return v < self.version
        if self.op == "!=":
            return v != self.version
        return v == self.version  # "=="


@dataclass(frozen=True, slots=True)
class VersionRange:
    """A conjunction (AND) of comparator terms; ``*`` matches everything."""

    raw: str
    _terms: tuple[tuple[_Comparator, ...], ...]

    @classmethod
    def parse(cls, text: str) -> VersionRange:
        """Parse a comma-separated range expression into comparator groups."""
        if not isinstance(text, str):
            raise PluginValidationError(f"version range must be a string: {text!r}")
        spec = text.strip()
        if spec in ("", "*", "x", "X"):
            return cls(raw=spec or "*", _terms=())
        groups: list[tuple[_Comparator, ...]] = []
        for part in spec.split(","):
            term = part.strip()
            if not term:
                raise PluginValidationError(f"empty term in version range: {text!r}")
            groups.append(_parse_term(term))
        return cls(raw=spec, _terms=tuple(groups))

    def matches(self, version: Version | str) -> bool:
        """True when ``version`` satisfies every comparator term (AND)."""
        v = version if isinstance(version, Version) else Version.parse(version)
        # By default a stable range excludes prereleases unless a term explicitly
        # names one (npm/cargo behaviour) — avoids 2.0.0-rc leaking into ^1.
        if v.is_prerelease and not self._allows_prerelease(v):
            return False
        return all(all(cmp.matches(v) for cmp in group) for group in self._terms)

    def _allows_prerelease(self, v: Version) -> bool:
        return any(
            cmp.version.is_prerelease and cmp.version.core == v.core
            for group in self._terms
            for cmp in group
        )

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.raw


def _parse_term(term: str) -> tuple[_Comparator, ...]:
    m = _OP_RE.match(term)
    if m is None:  # pragma: no cover - regex always matches non-empty
        raise PluginValidationError(f"invalid range term: {term!r}")
    op, rest = m.group(1) or "==", m.group(2).strip()

    if op == "^":
        return _caret(rest)
    if op == "~":
        return _tilde(rest)
    if op == "==" and _XRANGE_RE.match(rest) and ("x" in rest.lower() or "*" in rest):
        return _xrange(rest)
    return (_Comparator(op=op, version=Version.parse(rest)),)


def _xrange(rest: str) -> tuple[_Comparator, ...]:
    """``1.2.x`` / ``1.*`` → a half-open ``[lower, next)`` interval."""
    m = _XRANGE_RE.match(rest)
    assert m is not None
    major, minor, patch = m.group(1), m.group(2), m.group(3)
    if major in ("*", "x", "X", None):
        return ()  # matches everything
    maj = int(major)
    if minor in (None, "*", "x", "X"):
        lo, hi = Version(maj, 0, 0), Version(maj + 1, 0, 0)
    elif patch in (None, "*", "x", "X"):
        mn = int(minor)
        lo, hi = Version(maj, mn, 0), Version(maj, mn + 1, 0)
    else:  # pragma: no cover - exact version, not an x-range
        return (_Comparator("==", Version(maj, int(minor), int(patch))),)
    return (_Comparator(">=", lo), _Comparator("<", hi))


def _caret(rest: str) -> tuple[_Comparator, ...]:
    """``^1.2.3`` → ``>=1.2.3, <2.0.0`` (``^0.x`` pins the minor; ``^0.0.z`` the patch)."""
    base = Version.parse(_fill(rest))
    if base.major > 0:
        upper = Version(base.major + 1, 0, 0)
    elif base.minor > 0:
        upper = Version(0, base.minor + 1, 0)
    else:
        upper = Version(0, 0, base.patch + 1)
    return (_Comparator(">=", base), _Comparator("<", upper))


def _tilde(rest: str) -> tuple[_Comparator, ...]:
    """``~1.2.3`` → ``>=1.2.3, <1.3.0`` (allows patch-level changes)."""
    base = Version.parse(_fill(rest))
    upper = Version(base.major, base.minor + 1, 0)
    return (_Comparator(">=", base), _Comparator("<", upper))


def _fill(partial: str) -> str:
    """Zero-fill a partial version (``1`` → ``1.0.0``, ``1.2`` → ``1.2.0``)."""
    core = partial.split("-", 1)[0]
    pre = partial[len(core) :]
    bits = core.split(".")
    while len(bits) < 3:
        bits.append("0")
    return ".".join(bits[:3]) + pre


__all__ = ["Version", "VersionRange"]
