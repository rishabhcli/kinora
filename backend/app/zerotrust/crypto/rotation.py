"""Online, batched key rotation and re-encryption jobs.

Two rotation flavours, both designed to run *online* — concurrent with live
reads/writes — in bounded batches that can be checkpointed and resumed:

1. **KEK rotation (cheap, the common case).** Publish a new KEK version, then
   re-*wrap* every DEK under it (:meth:`Rotator.rewrap_batch`). The bulk
   ciphertext is never touched — only the small wrapped-DEK prefix is rewritten —
   so this is fast and the old KEK version can be destroyed once its DEK count
   reaches zero (crypto-retire). New writes already use the new version.

2. **DEK rotation / re-encryption (full).** Decrypt under the old DEK and
   re-encrypt under a brand-new DEK (:meth:`Rotator.reencrypt_batch`). Used when a
   DEK is suspected exposed, when migrating algorithms, or to satisfy a "re-key
   everything" policy. More expensive (touches ciphertext) but still batched.

Online-safety contract
-----------------------
A row is processed under a *compare-and-set*: the new payload is written back
**only if the stored payload still equals the one we read** (an optimistic
version check the caller supplies via :class:`RowCursor`). A concurrent writer
that changed the row between read and write wins; the rotator skips that row this
pass and the next pass picks up the (already-new-key) value. This makes rotation
idempotent and free of lost updates without holding long locks.

The KEK/DEK *unwrap* during rotation is permitted on a ``PENDING_DELETION`` KEK
version (decrypt-only), which is exactly the drain window the KMS state machine
opens on :meth:`SoftwareKMS.rotate_kek`.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass

from app.zerotrust.crypto.context import AssociatedData, CryptoProvider
from app.zerotrust.crypto.field import FieldSpec, StoredField


@dataclass(frozen=True, slots=True)
class RowRef:
    """A reference to one encrypted cell awaiting rotation.

    Attributes:
        row_id: the primary key (also the AAD record id for row-bound columns).
        payload: the current stored encrypted blob (output of ``StoredField``).
        table: owning table (for AAD).
        column: owning column (for AAD).
    """

    row_id: str
    payload: bytes
    table: str
    column: str

    def aad(self) -> AssociatedData:
        return AssociatedData(table=self.table, column=self.column, record_id=self.row_id)


@dataclass(frozen=True, slots=True)
class RotationOutcome:
    """The result of rotating one batch."""

    scanned: int
    rotated: int
    skipped_current: int  # already on the target key — no work needed
    skipped_conflict: int  # lost the compare-and-set to a concurrent writer

    def __add__(self, other: RotationOutcome) -> RotationOutcome:
        return RotationOutcome(
            scanned=self.scanned + other.scanned,
            rotated=self.rotated + other.rotated,
            skipped_current=self.skipped_current + other.skipped_current,
            skipped_conflict=self.skipped_conflict + other.skipped_conflict,
        )


#: A persistence callback the caller supplies: write ``new_payload`` to ``ref``'s
#: cell **iff** the stored payload still equals ``ref.payload`` (compare-and-set).
#: Returns True on success, False if a concurrent writer won (the rotator then
#: counts a conflict and moves on). This is the only seam the rotator needs into
#: the data store, keeping it storage-agnostic and unit-testable.
CompareAndSet = Callable[[RowRef, bytes], bool]


@dataclass
class Rotator:
    """Performs online, batched KEK re-wrap and DEK re-encryption."""

    provider: CryptoProvider
    compare_and_set: CompareAndSet

    # -- KEK rotation: re-wrap DEKs (ciphertext untouched) ------------------- #

    def rewrap_batch(self, rows: Iterable[RowRef]) -> RotationOutcome:
        """Re-wrap each row's DEK under the active KEK version.

        Only the wrapped-DEK metadata prefix changes; the AEAD envelope (the bulk
        ciphertext) is preserved byte-for-byte, so this is the cheap rotation.
        Rows already wrapped under the target version are counted as
        ``skipped_current``.
        """
        scanned = rotated = skipped_current = skipped_conflict = 0
        for ref in rows:
            scanned += 1
            stored = StoredField.from_bytes(ref.payload)
            wrapped = stored.ciphertext.wrapped_dek
            new_wrapped = self.provider.rewrap(wrapped)
            if new_wrapped.kek_version == wrapped.kek_version:
                skipped_current += 1
                continue
            new_stored = StoredField(
                ciphertext=type(stored.ciphertext)(
                    envelope=stored.ciphertext.envelope, wrapped_dek=new_wrapped
                )
            )
            if self.compare_and_set(ref, new_stored.to_bytes()):
                rotated += 1
            else:
                skipped_conflict += 1
        return RotationOutcome(scanned, rotated, skipped_current, skipped_conflict)

    # -- DEK rotation: decrypt + re-encrypt under a fresh DEK ---------------- #

    def reencrypt_batch(self, spec: FieldSpec, rows: Iterable[RowRef]) -> RotationOutcome:
        """Decrypt each row under its old DEK and re-encrypt under a fresh DEK.

        Used for suspected-exposure re-keying or algorithm migration. The
        plaintext lives in process memory only for the duration of one row's
        re-encryption. Honours the same compare-and-set online-safety contract.
        """
        from app.zerotrust.crypto.field import FieldEncryptor

        encryptor = FieldEncryptor(self.provider)
        scanned = rotated = skipped_conflict = 0
        for ref in rows:
            scanned += 1
            plaintext = encryptor.decrypt(spec, ref.payload, ref.aad())
            new_payload, _artifacts = encryptor.encrypt(spec, plaintext, ref.aad())
            if self.compare_and_set(ref, new_payload):
                rotated += 1
            else:
                skipped_conflict += 1
        return RotationOutcome(
            scanned, rotated, skipped_current=0, skipped_conflict=skipped_conflict
        )


def batched(items: Iterable[RowRef], size: int) -> Iterator[list[RowRef]]:
    """Yield ``items`` in lists of at most ``size`` (the rotation batch unit)."""
    if size <= 0:
        raise ValueError("batch size must be positive")
    batch: list[RowRef] = []
    for item in items:
        batch.append(item)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


__all__ = [
    "CompareAndSet",
    "RotationOutcome",
    "Rotator",
    "RowRef",
    "batched",
]
