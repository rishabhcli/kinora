"""Semantic versioning for the prompt registry.

The crew tags every prompt with a short *prompt-version* string like
``adapter@v1`` / ``cinematographer@v3`` (see ``app.agents.prompts``). Those tags
are great for logging but they are **not** orderable beyond a single integer and
they carry no notion of *what kind* of change happened. The registry layers a
real semantic version (``MAJOR.MINOR.PATCH``) on top so it can:

* order versions deterministically (rollback to "the previous one");
* express the *kind* of a change (a reworded guardrail is a PATCH; a new output
  field is a MINOR; a breaking schema change is a MAJOR);
* seed itself from the agents' existing tags without editing them — the ``vN``
  tag maps to ``N.0.0`` so ``adapter@v3`` seeds as ``3.0.0``.

This module is pure (no app imports) and fully unit-tested. It deliberately
implements only the subset of SemVer 2.0.0 the registry needs: numeric
``MAJOR.MINOR.PATCH`` with an optional dot-separated pre-release tail. Build
metadata (``+...``) is parsed and preserved but ignored for ordering, per spec.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import total_ordering

from app.llmops.errors import InvalidVersionError

#: The change kind a bump represents (drives :func:`bump`).
BumpKind = str  # one of "major" | "minor" | "patch"

_CORE = r"(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)"
_PRERELEASE = r"(?:-(?P<prerelease>[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
_BUILD = r"(?:\+(?P<build>[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
_SEMVER_RE = re.compile(rf"^{_CORE}{_PRERELEASE}{_BUILD}$")

#: Matches the agents' ``key@vN`` prompt tags (e.g. ``cinematographer@v3``).
_TAG_RE = re.compile(r"^(?P<key>[a-z0-9_]+)@v(?P<n>\d+)$")


def _cmp_prerelease(a: str, b: str) -> int:
    """Compare two pre-release strings per SemVer rule 11 (numeric < alphanumeric)."""
    if a == b:
        return 0
    # A version WITHOUT a pre-release outranks one with (handled by caller); here
    # both are non-empty.
    ai = a.split(".")
    bi = b.split(".")
    for x, y in zip(ai, bi, strict=False):
        x_num, y_num = x.isdigit(), y.isdigit()
        if x_num and y_num:
            xi, yi = int(x), int(y)
            if xi != yi:
                return -1 if xi < yi else 1
        elif x_num != y_num:
            # Numeric identifiers always have lower precedence than alphanumeric.
            return -1 if x_num else 1
        elif x != y:
            return -1 if x < y else 1
    # Longer pre-release wins when all shared identifiers are equal.
    if len(ai) != len(bi):
        return -1 if len(ai) < len(bi) else 1
    return 0


@total_ordering
@dataclass(frozen=True, slots=True)
class SemVer:
    """An orderable ``MAJOR.MINOR.PATCH`` (+ optional pre-release / build) version."""

    major: int
    minor: int
    patch: int
    prerelease: str | None = None
    build: str | None = None

    # -- construction -------------------------------------------------------- #

    @classmethod
    def parse(cls, text: str) -> SemVer:
        """Parse ``text`` into a :class:`SemVer` (raises :class:`InvalidVersionError`)."""
        match = _SEMVER_RE.match(text.strip())
        if match is None:
            raise InvalidVersionError(f"{text!r} is not a valid semantic version")
        return cls(
            major=int(match.group("major")),
            minor=int(match.group("minor")),
            patch=int(match.group("patch")),
            prerelease=match.group("prerelease"),
            build=match.group("build"),
        )

    @classmethod
    def from_prompt_tag(cls, tag: str) -> SemVer:
        """Map an agent prompt tag (``key@vN``) to ``N.0.0``.

        This is how the registry seeds itself from ``app.agents.prompts`` without
        touching those tags: ``adapter@v3`` ⇒ ``SemVer(3, 0, 0)``.
        """
        match = _TAG_RE.match(tag.strip())
        if match is None:
            raise InvalidVersionError(f"{tag!r} is not a valid prompt tag (expected key@vN)")
        return cls(major=int(match.group("n")), minor=0, patch=0)

    # -- ordering ------------------------------------------------------------ #

    @property
    def _core(self) -> tuple[int, int, int]:
        return (self.major, self.minor, self.patch)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, SemVer):
            return NotImplemented
        # Build metadata is ignored for equality/ordering (SemVer 2.0.0 §10).
        return self._core == other._core and self.prerelease == other.prerelease

    def __hash__(self) -> int:
        return hash((self._core, self.prerelease))

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, SemVer):
            return NotImplemented
        if self._core != other._core:
            return self._core < other._core
        # Equal core: a pre-release has LOWER precedence than the release.
        if self.prerelease is None and other.prerelease is None:
            return False
        if self.prerelease is None:  # release > pre-release
            return False
        if other.prerelease is None:
            return True
        return _cmp_prerelease(self.prerelease, other.prerelease) < 0

    # -- rendering ----------------------------------------------------------- #

    @property
    def is_prerelease(self) -> bool:
        return self.prerelease is not None

    def __str__(self) -> str:
        out = f"{self.major}.{self.minor}.{self.patch}"
        if self.prerelease is not None:
            out += f"-{self.prerelease}"
        if self.build is not None:
            out += f"+{self.build}"
        return out

    def bump(self, kind: BumpKind) -> SemVer:
        """Return the next version for a ``major`` / ``minor`` / ``patch`` change.

        Bumping clears any pre-release/build tail, matching the usual release flow
        (you bump *to* a clean released version).
        """
        normalized = kind.strip().lower()
        if normalized == "major":
            return SemVer(self.major + 1, 0, 0)
        if normalized == "minor":
            return SemVer(self.major, self.minor + 1, 0)
        if normalized == "patch":
            return SemVer(self.major, self.minor, self.patch + 1)
        raise InvalidVersionError(f"unknown bump kind {kind!r} (want major|minor|patch)")


def parse(text: str) -> SemVer:
    """Module-level shortcut for :meth:`SemVer.parse`."""
    return SemVer.parse(text)


def is_valid(text: str) -> bool:
    """True iff ``text`` is a valid semantic version (never raises)."""
    try:
        SemVer.parse(text)
    except InvalidVersionError:
        return False
    return True


def latest(versions: list[str]) -> str:
    """Return the highest semantic version from ``versions`` (string in, string out).

    Raises :class:`InvalidVersionError` on the first malformed entry and
    :class:`ValueError` on an empty list.
    """
    if not versions:
        raise ValueError("latest() requires at least one version")
    parsed = [(SemVer.parse(v), v) for v in versions]
    parsed.sort(key=lambda pair: pair[0])
    return parsed[-1][1]


def sort_versions(versions: list[str], *, descending: bool = False) -> list[str]:
    """Return ``versions`` sorted by semantic precedence (ascending by default)."""
    parsed = sorted(((SemVer.parse(v), v) for v in versions), key=lambda p: p[0])
    ordered = [v for _, v in parsed]
    return list(reversed(ordered)) if descending else ordered


__all__ = [
    "BumpKind",
    "SemVer",
    "is_valid",
    "latest",
    "parse",
    "sort_versions",
]
