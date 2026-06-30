"""The store-seam protocols the privacy subsystem orchestrates against.

The DSAR-export, right-to-erasure and residual-scan machinery never touches a
concrete store directly — it talks to these narrow :class:`typing.Protocol`
seams. That keeps the orchestrator pure policy (driven by the
:mod:`~app.privacy.datamap`) and lets the deterministic test suite supply
in-memory fakes for every store (no Postgres, no MinIO, no Redis, no network).

The four seams correspond to the four :class:`~app.privacy.enums.StoreKind`
families:

* :class:`SubjectDataStore` — a queryable + (hard-delete | anonymize)-able store
  (relational rows; the same seam serves a derived index, best-effort);
* :class:`BlobStore` — object storage (list + delete by prefix);
* :class:`EventStore` — the append-only domain event store (crypto-erase a
  subject's per-record key; the chain stays intact because the ciphertext stays);
* :class:`AuditRedactor` — the **local** protocol the hash-chained audit/compliance
  log will later satisfy. Round 1's audit-log subsystem (with its
  redaction-preserving chain) is a sibling we cannot import in this round, so we
  define the contract here; the concrete log implements ``redact_subject`` /
  ``scan_subject`` / ``verify_chain`` and the privacy erasure orchestrator binds
  to it without either package depending on the other.

All methods are ``async`` (real stores are I/O-bound); fakes implement them with
trivial in-memory coroutines.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class SubjectDataStore(Protocol):
    """A relational-style store keyed to subjects (rows that can be read/removed).

    Implemented by a thin adapter over the real Postgres repositories; faked
    in-memory for tests. ``resource`` is a logical table name; ``subject_locator``
    is the column whose value identifies the subject (from the data-map).
    """

    async def fetch_subject_rows(
        self, *, resource: str, subject_locator: str, subject_id: str
    ) -> list[dict[str, Any]]:
        """Return every row of ``resource`` belonging to ``subject_id`` (for export)."""
        ...

    async def hard_delete(
        self, *, resource: str, subject_locator: str, subject_id: str
    ) -> int:
        """Physically delete the subject's rows of ``resource``; return the count.

        Must be idempotent: a second call on an already-cleared subject deletes
        nothing and returns ``0`` (so a resumed run is safe).
        """
        ...

    async def anonymize(
        self,
        *,
        resource: str,
        subject_locator: str,
        subject_id: str,
        fields: list[str],
        tombstone: str,
    ) -> int:
        """Overwrite ``fields`` of the subject's rows with ``tombstone`` in place.

        Returns the number of rows touched. Idempotent: re-anonymising an
        already-tombstoned row is a no-op that still reports the matched rows.
        """
        ...

    async def count_subject_rows(
        self, *, resource: str, subject_locator: str, subject_id: str
    ) -> int:
        """Count rows still attributable to ``subject_id`` (the residual scan)."""
        ...


@runtime_checkable
class BlobStore(Protocol):
    """Object storage (clips, keyframes, narration, source PDFs)."""

    async def list_prefix(self, prefix: str) -> list[str]:
        """List object keys under ``prefix``."""
        ...

    async def delete_prefix(self, prefix: str) -> int:
        """Delete every object under ``prefix``; return the count. Idempotent."""
        ...


@runtime_checkable
class EventStore(Protocol):
    """The append-only domain event store (crypto-erasure seam).

    Events are immutable, so a subject's data is removed by destroying the
    per-subject (or per-stream) encryption key: the ciphertext payloads stay in
    place (the offsets / hashes that downstream projections committed to are
    untouched) but become permanently unreadable.
    """

    async def list_subject_streams(self, *, subject_id: str) -> list[str]:
        """Stream ids that carry the subject's data (for the scan + key destruction)."""
        ...

    async def crypto_erase_subject(self, *, subject_id: str) -> int:
        """Destroy the subject's encryption key(s); return # of streams affected.

        Idempotent: once the key is gone, a repeat call affects ``0`` streams.
        """
        ...

    async def subject_recoverable(self, *, subject_id: str) -> bool:
        """Whether the subject's events are still decryptable (residual scan)."""
        ...


@runtime_checkable
class AuditRedactor(Protocol):
    """LOCAL contract for a hash-chained audit/compliance log (right-to-erasure).

    The audit log is append-only **and** tamper-evident: each entry's hash commits
    to the previous entry's hash plus its own content, so a verifier can detect any
    retroactive edit. Deleting a subject's entry would break every following hash.
    The compliant move is **redaction that preserves the chain**: replace the
    personal fields inside the entry's content with a redaction marker, re-derive
    that entry's hash over the redacted content, and re-chain every subsequent
    entry so the whole chain still verifies — the *fact* that an audited event
    happened is retained for accountability, only the personal payload is gone.

    Round 1's audit-log subsystem owns the concrete redaction-preserving chain; we
    declare the protocol here so this round's privacy orchestrator can drive it
    (or a test fake) without importing the sibling. A conforming implementation
    must keep :meth:`verify_chain` returning ``True`` across a :meth:`redact_subject`.
    """

    async def redact_subject(
        self, *, subject_id: str, redaction_marker: str = "[REDACTED]"
    ) -> int:
        """Redact the subject's personal fields in every entry; return # redacted.

        Re-derives the affected entries' hashes and re-chains the tail so the chain
        still verifies. Idempotent: re-redacting an already-redacted entry is a
        no-op (the marker is already in place).
        """
        ...

    async def scan_subject(self, *, subject_id: str) -> int:
        """Count entries that still contain the subject's personal data (residual scan)."""
        ...

    async def verify_chain(self) -> bool:
        """Re-hash the whole chain; ``True`` iff it is intact (no tamper detected)."""
        ...


__all__ = [
    "AuditRedactor",
    "BlobStore",
    "EventStore",
    "SubjectDataStore",
]
