"""Typed exceptions for the data-portability layer.

Every failure in export/import/backup/migration is one of these, so the API
route can map them to a stable error envelope (kinora.md §12) and tests can
assert on a precise type rather than a message substring.
"""

from __future__ import annotations


class PortabilityError(Exception):
    """Base class for every data-portability failure."""


class ArchiveFormatError(PortabilityError):
    """The archive is structurally malformed (bad zip, missing manifest, bad JSON).

    Distinct from a *checksum* mismatch: the bytes parse but the layout is wrong,
    or the bytes do not parse at all.
    """


class ChecksumMismatchError(PortabilityError):
    """A stored checksum does not match the recomputed digest of its payload.

    Carries the offending member name and both digests so a verifier can report
    exactly which entry was truncated or tampered with.
    """

    def __init__(self, member: str, expected: str, actual: str) -> None:
        super().__init__(
            f"checksum mismatch for {member!r}: expected {expected}, got {actual}"
        )
        self.member = member
        self.expected = expected
        self.actual = actual


class UnsupportedArchiveVersionError(PortabilityError):
    """The archive's ``format_version`` cannot be migrated to the current one.

    Raised when the version is newer than this build understands, or older than
    the oldest version the migration chain still supports.
    """

    def __init__(self, found: int, supported: tuple[int, int]) -> None:
        lo, hi = supported
        super().__init__(
            f"unsupported archive format_version {found} "
            f"(this build reads {lo}..{hi})"
        )
        self.found = found
        self.supported = supported


class ReferentialIntegrityError(PortabilityError):
    """An imported archive references an id/key that the archive does not contain.

    Import fails closed on a dangling reference so a partial graph is never
    written to the database.
    """

    def __init__(self, table: str, column: str, value: str) -> None:
        super().__init__(
            f"dangling reference in {table}.{column}: {value!r} not present in archive"
        )
        self.table = table
        self.column = column
        self.value = value


class ArchiveKindMismatchError(PortabilityError):
    """The archive's declared ``kind`` is not the one the caller asked to import.

    e.g. importing a canon-only archive through the full book-bundle path.
    """

    def __init__(self, expected: str, found: str) -> None:
        super().__init__(f"expected a {expected!r} archive, got {found!r}")
        self.expected = expected
        self.found = found


__all__ = [
    "ArchiveFormatError",
    "ArchiveKindMismatchError",
    "ChecksumMismatchError",
    "PortabilityError",
    "ReferentialIntegrityError",
    "UnsupportedArchiveVersionError",
]
