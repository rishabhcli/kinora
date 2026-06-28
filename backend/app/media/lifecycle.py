"""Retention policy + lifecycle GC sweep.

Two pieces:

* **Policy (pure)** — :class:`RetentionPolicy` decides, for a given asset kind,
  whether and when it should expire. *Derived* assets (poster/sprite/HLS/…) are
  cheap to regenerate, so they get a finite retention horizon; *primary* assets
  (clips/scenes/source/audio/keyframes) are expensive and never auto-collected.
  This is the lifecycle half of §8.7 ("a re-read costs nothing"): we can drop
  derivatives freely because they rebuild from the cached primary.

* **Sweep (DB + store)** — :func:`sweep_expired` walks the repository's expired,
  orphaned (``ref_count == 0``) derived assets, deletes the object-store blob,
  and removes the row. It is idempotent (a missing blob is fine) and bounded by
  a batch size so a worker can run it on a timer without long transactions.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from app.core.logging import get_logger
from app.media.kinds import DERIVED_KINDS, MediaAssetKind, is_derived
from app.media.repository import MediaAssetRepo
from app.media.store import MediaStore

logger = get_logger("app.media.lifecycle")


@dataclass(frozen=True, slots=True)
class RetentionPolicy:
    """How long derived assets live before they may be collected."""

    derived_retention_days: int = 30

    def expires_at(self, kind: MediaAssetKind, *, now: datetime) -> datetime | None:
        """The retention horizon for a freshly-created asset of ``kind``.

        Returns ``None`` (keep indefinitely) for primary kinds or when retention
        is disabled (``derived_retention_days <= 0``); otherwise ``now + N days``
        for derived kinds.
        """
        if self.derived_retention_days <= 0 or not is_derived(kind):
            return None
        return now + timedelta(days=self.derived_retention_days)

    @property
    def is_enabled(self) -> bool:
        """True when derived assets are given a finite horizon."""
        return self.derived_retention_days > 0


@dataclass(frozen=True, slots=True)
class SweepResult:
    """The outcome of one GC sweep pass."""

    collected: int
    bytes_freed: int
    keys: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class IntegrityReport:
    """The outcome of a checksum integrity sweep over a set of assets."""

    checked: int
    ok: int
    missing: tuple[str, ...]
    corrupt: tuple[str, ...]

    @property
    def healthy(self) -> bool:
        """True when nothing was missing or corrupt."""
        return not self.missing and not self.corrupt


async def verify_integrity(
    repo: MediaAssetRepo,
    store: MediaStore,
    *,
    book_id: str,
    kind: MediaAssetKind | None = None,
) -> IntegrityReport:
    """Re-hash a book's stored blobs and compare to their recorded hashes.

    Detects silent corruption / drift between the object store and the registry.
    An asset with no recorded ``content_hash`` is skipped (nothing to compare);
    a key absent from the store is reported as *missing*; a hash mismatch is
    reported as *corrupt*. Read-only — never mutates the store or registry.
    """
    from app.media.hashing import sha256_hex

    assets = await repo.list_for_book(book_id, kind=kind)
    checked = ok = 0
    missing: list[str] = []
    corrupt: list[str] = []
    for asset in assets:
        if not asset.content_hash:
            continue
        checked += 1
        try:
            data = store.get(asset.storage_key)
        except Exception:  # noqa: BLE001 - any backend miss → missing
            missing.append(asset.storage_key)
            continue
        if sha256_hex(data) == asset.content_hash:
            ok += 1
        else:
            corrupt.append(asset.storage_key)
            logger.warning("media.integrity.corrupt", key=asset.storage_key)
    return IntegrityReport(
        checked=checked, ok=ok, missing=tuple(missing), corrupt=tuple(corrupt)
    )


async def sweep_expired(
    repo: MediaAssetRepo,
    store: MediaStore,
    *,
    now: datetime,
    batch: int = 100,
) -> SweepResult:
    """Collect expired, orphaned **derived** assets (blob + row).

    Only derived kinds are eligible (primary assets are never auto-collected);
    only rows with ``expires_at <= now`` and ``ref_count == 0`` are touched.
    Object-store deletes are idempotent, so a partially-collected asset (blob
    gone, row present) is cleaned up on the next pass. Returns a
    :class:`SweepResult` for observability.
    """
    expired = await repo.list_expired(now=now, kinds=list(DERIVED_KINDS), limit=batch)
    collected = 0
    bytes_freed = 0
    keys: list[str] = []
    for asset in expired:
        try:
            store.delete(asset.storage_key)
        except Exception:  # noqa: BLE001 - a missing/erroring blob must not stall GC
            logger.warning("media.gc.delete_failed", key=asset.storage_key)
        await repo.delete(asset.id)
        collected += 1
        bytes_freed += int(asset.size_bytes or 0)
        keys.append(asset.storage_key)
    if collected:
        logger.info("media.gc.swept", collected=collected, bytes_freed=bytes_freed)
    return SweepResult(collected=collected, bytes_freed=bytes_freed, keys=tuple(keys))


__all__ = [
    "IntegrityReport",
    "RetentionPolicy",
    "SweepResult",
    "sweep_expired",
    "verify_integrity",
]
