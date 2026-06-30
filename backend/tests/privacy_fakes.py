"""Deterministic in-memory fakes for the privacy store seams.

These implement the :mod:`app.privacy.protocols` contracts with plain dicts/lists
— no Postgres, no MinIO, no Redis, no network — so the privacy subsystem's
export / erasure / scan / crypto-erasure flows can be tested fully and
deterministically. Each fake is intentionally simple but *honest* about the
semantics the orchestrator relies on:

* every mutating method is **idempotent** (a second call is a no-op);
* :class:`FakeAuditLog` implements a real hash-chain so redaction can be shown to
  *preserve* chain integrity (the whole point of the audit-redactor seam);
* :class:`FakeEventStore` models crypto-erasure as destroying a per-subject key,
  after which the subject's events are unrecoverable but still present.
"""

from __future__ import annotations

from typing import Any

from app.privacy.hashchain import GENESIS_PREV_HASH, chain_hash


class FakeSubjectStore:
    """In-memory relational store: ``resource -> list[row]`` (rows are dicts)."""

    def __init__(self, tables: dict[str, list[dict[str, Any]]] | None = None) -> None:
        self.tables: dict[str, list[dict[str, Any]]] = {
            k: [dict(r) for r in v] for k, v in (tables or {}).items()
        }

    def _matches(self, row: dict[str, Any], locator: str, subject_id: str) -> bool:
        return row.get(locator) == subject_id

    async def fetch_subject_rows(
        self, *, resource: str, subject_locator: str, subject_id: str
    ) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in self.tables.get(resource, [])
            if self._matches(r, subject_locator, subject_id)
        ]

    async def hard_delete(
        self, *, resource: str, subject_locator: str, subject_id: str
    ) -> int:
        rows = self.tables.get(resource, [])
        keep = [r for r in rows if not self._matches(r, subject_locator, subject_id)]
        removed = len(rows) - len(keep)
        self.tables[resource] = keep
        return removed

    async def anonymize(
        self,
        *,
        resource: str,
        subject_locator: str,
        subject_id: str,
        fields: list[str],
        tombstone: str,
    ) -> int:
        touched = 0
        for r in self.tables.get(resource, []):
            if self._matches(r, subject_locator, subject_id):
                touched += 1
                for f in fields:
                    r[f] = tombstone
                # Detach the row from the subject so the residual scan reports 0.
                r[subject_locator] = f"anon:{tombstone}"
        return touched

    async def count_subject_rows(
        self, *, resource: str, subject_locator: str, subject_id: str
    ) -> int:
        return sum(
            1
            for r in self.tables.get(resource, [])
            if self._matches(r, subject_locator, subject_id)
        )


class FakeBlobStore:
    """In-memory object store: a flat set of object keys."""

    def __init__(self, keys: list[str] | None = None) -> None:
        self.keys: set[str] = set(keys or [])

    async def list_prefix(self, prefix: str) -> list[str]:
        return sorted(k for k in self.keys if k.startswith(prefix))

    async def delete_prefix(self, prefix: str) -> int:
        gone = {k for k in self.keys if k.startswith(prefix)}
        self.keys -= gone
        return len(gone)


class FakeEventStore:
    """In-memory append-only event store with per-subject crypto-erasure.

    Events are immutable; a subject's events become unrecoverable when their key
    is destroyed. ``erased`` records destroyed keys so the operation is idempotent.
    """

    def __init__(self, streams: dict[str, list[str]] | None = None) -> None:
        # subject_id -> [stream_id, ...]
        self.streams: dict[str, list[str]] = {
            k: list(v) for k, v in (streams or {}).items()
        }
        self.erased: set[str] = set()

    async def list_subject_streams(self, *, subject_id: str) -> list[str]:
        return list(self.streams.get(subject_id, []))

    async def crypto_erase_subject(self, *, subject_id: str) -> int:
        if subject_id in self.erased:
            return 0  # idempotent: key already destroyed
        n = len(self.streams.get(subject_id, []))
        if n:
            self.erased.add(subject_id)
        return n

    async def subject_recoverable(self, *, subject_id: str) -> bool:
        return bool(self.streams.get(subject_id)) and subject_id not in self.erased


class _AuditEntry:
    """One hash-chained audit entry (mutable content, recomputable hash)."""

    __slots__ = ("seq", "subject_id", "content", "prev_hash", "entry_hash")

    def __init__(
        self, seq: int, subject_id: str | None, content: dict[str, Any]
    ) -> None:
        self.seq = seq
        self.subject_id = subject_id
        self.content = content
        self.prev_hash = GENESIS_PREV_HASH
        self.entry_hash = ""

    def core(self) -> dict[str, Any]:
        return {"seq": self.seq, "subject_id": self.subject_id, "content": self.content}


class FakeAuditLog:
    """A hash-chained audit log that redacts a subject while preserving the chain.

    Demonstrates the :class:`~app.privacy.protocols.AuditRedactor` contract: redact
    replaces the subject's personal fields with the marker, re-derives that entry's
    hash, and re-chains every following entry so :meth:`verify_chain` stays ``True``.
    The personal fields it knows to redact are the ones the data-map declares for
    the ``audit_log`` store (here: ``email`` / ``ip``).
    """

    #: Personal fields a redaction clears (mirrors the data-map's audit fields).
    PII_FIELDS = ("email", "ip")

    def __init__(self) -> None:
        self.entries: list[_AuditEntry] = []
        self._next_seq = 1

    def append(self, *, subject_id: str | None, content: dict[str, Any]) -> None:
        """Append a new entry and (re)chain it onto the tail."""
        entry = _AuditEntry(self._next_seq, subject_id, dict(content))
        self._next_seq += 1
        self.entries.append(entry)
        self._rechain()

    def _rechain(self) -> None:
        prev = GENESIS_PREV_HASH
        for e in self.entries:
            e.prev_hash = prev
            e.entry_hash = chain_hash(prev, e.core())
            prev = e.entry_hash

    def _entry_has_pii(self, e: _AuditEntry) -> bool:
        if e.subject_id is None:
            return False
        return any(
            f in e.content and e.content[f] != "[REDACTED]" for f in self.PII_FIELDS
        )

    # --- AuditRedactor protocol -------------------------------------------- #

    async def redact_subject(
        self, *, subject_id: str, redaction_marker: str = "[REDACTED]"
    ) -> int:
        redacted = 0
        changed = False
        for e in self.entries:
            if e.subject_id == subject_id:
                touched = False
                for f in self.PII_FIELDS:
                    if f in e.content and e.content[f] != redaction_marker:
                        e.content[f] = redaction_marker
                        touched = True
                if touched:
                    redacted += 1
                    changed = True
        if changed:
            self._rechain()  # re-derive hashes; chain stays verifiable
        return redacted

    async def scan_subject(self, *, subject_id: str) -> int:
        return sum(
            1
            for e in self.entries
            if e.subject_id == subject_id and self._entry_has_pii(e)
        )

    async def verify_chain(self) -> bool:
        prev = GENESIS_PREV_HASH
        for e in self.entries:
            if e.prev_hash != prev:
                return False
            if e.entry_hash != chain_hash(prev, e.core()):
                return False
            prev = e.entry_hash
        return True


def make_populated_stores(subject_id: str = "user-1") -> dict[str, Any]:
    """Build a coherent set of fakes pre-loaded with one subject's data.

    Returns a dict of the four fakes plus the ``other`` subject id, so tests can
    assert that erasing one subject leaves the other untouched.
    """
    other = "user-2"
    subject_store = FakeSubjectStore(
        tables={
            "users": [
                {"id": subject_id, "email": "a@x.io", "display_name": "Ann", "password_hash": "h1"},
                {"id": other, "email": "b@x.io", "display_name": "Bo", "password_hash": "h2"},
            ],
            "books": [
                {"id": "book-1", "owner_id": subject_id, "title": "My Memoir"},
                {"id": "book-2", "owner_id": other, "title": "Their Book"},
            ],
            "reading_sessions": [
                {"id": "s1", "user_id": subject_id, "trajectory": [1, 2, 3]},
            ],
            "directing_preferences": [
                {"id": "p1", "user_id": subject_id, "profile": {"mood": "noir"}},
            ],
        }
    )
    blob_store = FakeBlobStore(
        keys=[
            f"books/{subject_id}/source.pdf",
            f"clips/{subject_id}/shot-1.mp4",
            f"clips/{subject_id}/shot-2.mp4",
            f"books/{other}/source.pdf",
        ]
    )
    event_store = FakeEventStore(
        streams={
            subject_id: ["book.uploaded:book-1", "reading.session_recorded:s1"],
            other: ["book.uploaded:book-2"],
        }
    )
    audit_log = FakeAuditLog()
    audit_log.append(
        subject_id=subject_id,
        content={"event": "login", "email": "a@x.io", "ip": "10.0.0.1"},
    )
    audit_log.append(
        subject_id=other,
        content={"event": "login", "email": "b@x.io", "ip": "10.0.0.2"},
    )
    audit_log.append(
        subject_id=subject_id,
        content={"event": "upload", "email": "a@x.io", "ip": "10.0.0.1"},
    )
    audit_log.append(subject_id=None, content={"event": "system_boot"})
    return {
        "subject_id": subject_id,
        "other": other,
        "subject_store": subject_store,
        "blob_store": blob_store,
        "event_store": event_store,
        "audit_log": audit_log,
    }


__all__ = [
    "FakeAuditLog",
    "FakeBlobStore",
    "FakeEventStore",
    "FakeSubjectStore",
    "make_populated_stores",
]
