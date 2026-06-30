"""A small, dependency-free semantic-version implementation for the mesh.

Inter-service message schemas are versioned with semver so the compatibility
checker and the converter chain can reason about *direction* (an upgrade vs a
downgrade) and *severity* (a MAJOR bump signals an intentional break). We do not
pull in an external semver package: the surface we need (parse, compare, bump,
and a tiny range spec) is small, must stay import-cheap, and must be totally
deterministic for the test suite.

The grammar is a pragmatic subset of SemVer 2.0.0: ``MAJOR.MINOR.PATCH`` with an
optional ``-prerelease`` and ``+build`` suffix. Build metadata is ignored for
ordering (per the spec); prerelease tags order *below* their release.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import total_ordering

from app.servicemesh.errors import VersionRangeError

__all__ = ["SemVer", "VersionRange", "BumpKind"]

# MAJOR.MINOR.PATCH(-prerelease)?(+build)?
_SEMVER_RE = re.compile(
    r"^(?P<major>0|[1-9]\d*)\."
    r"(?P<minor>0|[1-9]\d*)\."
    r"(?P<patch>0|[1-9]\d*)"
    r"(?:-(?P<prerelease>[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+(?P<build>[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)

BumpKind = str  # one of "major" | "minor" | "patch"


def _cmp_prerelease(a: tuple[str, ...], b: tuple[str, ...]) -> int:
    """Compare two prerelease identifier tuples per SemVer §11.

    An empty tuple (a release) sorts *above* any non-empty tuple (a prerelease).
    """
    if not a and not b:
        return 0
    if not a:  # release > prerelease
        return 1
    if not b:
        return -1
    for x, y in zip(a, b, strict=False):
        if x == y:
            continue
        x_num, y_num = x.isdigit(), y.isdigit()
        if x_num and y_num:
            return -1 if int(x) < int(y) else 1
        if x_num != y_num:  # numeric identifiers sort below alphanumeric
            return -1 if x_num else 1
        return -1 if x < y else 1
    # All shared identifiers equal -> the longer tuple is greater.
    return (len(a) > len(b)) - (len(a) < len(b))


@total_ordering
@dataclass(frozen=True, slots=True)
class SemVer:
    """An immutable, orderable semantic version."""

    major: int
    minor: int
    patch: int
    prerelease: tuple[str, ...] = ()
    build: tuple[str, ...] = ()

    # -- construction ------------------------------------------------------- #
    @classmethod
    def parse(cls, text: str) -> SemVer:
        """Parse ``MAJOR.MINOR.PATCH(-pre)?(+build)?`` into a :class:`SemVer`."""
        match = _SEMVER_RE.match(text.strip())
        if match is None:
            raise VersionRangeError(f"not a semantic version: {text!r}")
        pre = match.group("prerelease")
        build = match.group("build")
        return cls(
            major=int(match.group("major")),
            minor=int(match.group("minor")),
            patch=int(match.group("patch")),
            prerelease=tuple(pre.split(".")) if pre else (),
            build=tuple(build.split(".")) if build else (),
        )

    @classmethod
    def coerce(cls, value: SemVer | str) -> SemVer:
        """Accept either a :class:`SemVer` or its string form."""
        return value if isinstance(value, SemVer) else cls.parse(value)

    # -- ordering ----------------------------------------------------------- #
    @property
    def _core(self) -> tuple[int, int, int]:
        return (self.major, self.minor, self.patch)

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, SemVer):
            return NotImplemented
        if self._core != other._core:
            return self._core < other._core
        return _cmp_prerelease(self.prerelease, other.prerelease) < 0

    def __eq__(self, other: object) -> bool:
        # Build metadata is ignored for equality/precedence (SemVer §10).
        if not isinstance(other, SemVer):
            return NotImplemented
        return self._core == other._core and self.prerelease == other.prerelease

    def __hash__(self) -> int:
        return hash((self._core, self.prerelease))

    # -- rendering ---------------------------------------------------------- #
    def __str__(self) -> str:
        out = f"{self.major}.{self.minor}.{self.patch}"
        if self.prerelease:
            out += "-" + ".".join(self.prerelease)
        if self.build:
            out += "+" + ".".join(self.build)
        return out

    # -- evolution ---------------------------------------------------------- #
    @property
    def is_stable(self) -> bool:
        """A version is stable once it reaches ``1.0.0`` and is not a prerelease.

        Pre-1.0 schemas may break on a MINOR bump; the compatibility gate uses
        this to decide whether a channel is held to the strict contract.
        """
        return self.major >= 1 and not self.prerelease

    def bump(self, kind: BumpKind) -> SemVer:
        """Return the next version after a major/minor/patch increment."""
        if kind == "major":
            return SemVer(self.major + 1, 0, 0)
        if kind == "minor":
            return SemVer(self.major, self.minor + 1, 0)
        if kind == "patch":
            return SemVer(self.major, self.minor, self.patch + 1)
        raise VersionRangeError(f"unknown bump kind: {kind!r}")

    def same_major(self, other: SemVer) -> bool:
        """True when both share a MAJOR line (the compatibility envelope)."""
        return self.major == other.major


@dataclass(frozen=True, slots=True)
class VersionRange:
    """A closed/half-open ``[min, max)`` version range plus an optional pin.

    Used by capability negotiation: a role advertises the versions it can speak as
    a range, and the negotiator intersects two roles' ranges. ``max_exclusive`` of
    ``None`` means "unbounded above"; a pin (``==``) collapses to a single point.
    """

    min_inclusive: SemVer
    max_exclusive: SemVer | None = None

    @classmethod
    def parse(cls, spec: str) -> VersionRange:
        """Parse ``>=1.2.0,<2.0.0`` / ``==1.4.0`` / ``>=1.0.0`` style specs."""
        spec = spec.strip()
        if not spec:
            raise VersionRangeError("empty version range")
        if spec.startswith("=="):
            pin = SemVer.parse(spec[2:])
            return cls(min_inclusive=pin, max_exclusive=pin.bump("patch"))
        lo: SemVer | None = None
        hi: SemVer | None = None
        for clause in spec.split(","):
            clause = clause.strip()
            if clause.startswith(">="):
                lo = SemVer.parse(clause[2:])
            elif clause.startswith("<"):
                hi = SemVer.parse(clause[1:])
            else:
                raise VersionRangeError(f"unsupported range clause: {clause!r}")
        if lo is None:
            raise VersionRangeError(f"range must have a lower bound: {spec!r}")
        if hi is not None and hi <= lo:
            raise VersionRangeError(f"empty range: {spec!r}")
        return cls(min_inclusive=lo, max_exclusive=hi)

    def contains(self, version: SemVer) -> bool:
        """Membership test for the half-open interval."""
        if version < self.min_inclusive:
            return False
        return self.max_exclusive is None or version < self.max_exclusive

    def intersect(self, other: VersionRange) -> VersionRange | None:
        """The overlap of two ranges, or ``None`` when they are disjoint."""
        lo = max(self.min_inclusive, other.min_inclusive)
        if self.max_exclusive is None:
            hi = other.max_exclusive
        elif other.max_exclusive is None:
            hi = self.max_exclusive
        else:
            hi = min(self.max_exclusive, other.max_exclusive)
        if hi is not None and hi <= lo:
            return None
        return VersionRange(min_inclusive=lo, max_exclusive=hi)

    def __str__(self) -> str:
        if self.max_exclusive is not None and self.max_exclusive == self.min_inclusive.bump(
            "patch"
        ):
            return f"=={self.min_inclusive}"
        if self.max_exclusive is None:
            return f">={self.min_inclusive}"
        return f">={self.min_inclusive},<{self.max_exclusive}"
