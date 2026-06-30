"""Exception hierarchy for the disaster-recovery engine.

Every failure mode a backup or restore can hit is a typed subclass of
:class:`DRError` so callers (and the health report) can distinguish, say, a
corrupted segment (:class:`IntegrityError`) from a broken restore chain
(:class:`ChainError`) from a missing asset (:class:`AssetMismatchError`). Nothing
here is a network error — the seams are injected, so I/O failures surface as
whatever the injected store raises and are wrapped at the service boundary.
"""

from __future__ import annotations


class DRError(RuntimeError):
    """Base class for every disaster-recovery failure."""


class SnapshotError(DRError):
    """A consistent snapshot could not be captured (e.g. the pin moved)."""


class IntegrityError(DRError):
    """A checksum mismatch — a segment's bytes do not match its recorded digest.

    Carries the segment name and the expected/actual digests so the health
    report and the operator can see exactly which segment is corrupt.
    """

    def __init__(self, segment: str, expected: str, actual: str) -> None:
        self.segment = segment
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"integrity check failed for segment {segment!r}: "
            f"expected {expected}, computed {actual}"
        )


class ChainError(DRError):
    """A restore chain is broken — a referenced parent backup is missing/wrong.

    Raised when an incremental's ``parent_id`` does not resolve, when the chain
    does not terminate at a full backup, or when the chain's pinned positions are
    not strictly increasing (a malformed/forged chain).
    """


class AssetMismatchError(DRError):
    """The captured asset manifest does not match the asset source on restore.

    Carries the set of missing keys (referenced by the snapshot but absent from
    the source) so a dry-run can report exactly which assets are unrecoverable.
    """

    def __init__(self, missing: tuple[str, ...]) -> None:
        self.missing = missing
        preview = ", ".join(missing[:5])
        more = "" if len(missing) <= 5 else f" (+{len(missing) - 5} more)"
        super().__init__(f"{len(missing)} asset(s) missing on restore: {preview}{more}")


class RestoreError(DRError):
    """A restore could not be completed (replay/rebuild failed or verify failed)."""


class PointInTimeError(DRError):
    """A point-in-time target could not be resolved to a recoverable plan.

    Raised when ``T`` precedes every backup (no recoverable point), when no
    backup chain covers ``T``, or when a timestamp ``T`` cannot be mapped to an
    event position because the log has no event at/before it.
    """


class RetentionError(DRError):
    """A retention/GC operation would violate an invariant (e.g. orphan a chain)."""


class ManifestError(DRError):
    """A backup-set manifest is structurally invalid or self-inconsistent."""


__all__ = [
    "AssetMismatchError",
    "ChainError",
    "DRError",
    "IntegrityError",
    "ManifestError",
    "PointInTimeError",
    "RestoreError",
    "RetentionError",
    "SnapshotError",
]
