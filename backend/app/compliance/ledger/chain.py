"""The hash-chain primitive backing the compliance ledger.

Each ledger entry's ``entry_hash`` covers the previous entry's hash plus a
*canonicalised* projection of the entry's own content::

    entry_hash = sha256( prev_hash || canonical_json(payload_core) )

Canonicalisation (sorted keys, no whitespace, UTF-8) makes the hash stable
regardless of dict ordering, so a verifier can re-derive the whole chain and
detect any retroactive edit — the same tamper-evidence design as
:class:`app.db.models.bitemporal.CanonAudit`, generalised across every
compliance category.

This module is pure (no DB, no I/O) so it is trivially unit-testable.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

#: The sentinel ``prev_hash`` of the very first entry in the chain (genesis).
GENESIS_PREV_HASH = "0" * 64


def canonical_json(value: Any) -> str:
    """Serialise ``value`` deterministically (sorted keys, compact, UTF-8 safe).

    ``default=str`` lets datetimes / enums fall back to their string form so the
    projection never raises on a value the JSON encoder cannot natively handle.
    """
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )


def sha256_hex(data: str) -> str:
    """Return the hex SHA-256 of a UTF-8 string."""
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def payload_core(
    *,
    seq: int,
    category: str,
    event: str,
    subject_id: str | None,
    actor_id: str,
    payload: dict[str, Any] | None,
) -> dict[str, Any]:
    """The canonical projection of an entry's content that the hash commits to.

    Deliberately excludes ``created_at`` and the row ``id`` (which are assigned by
    the DB and not part of the logical event), so the chain is reproducible from
    the logical fields alone.
    """
    return {
        "seq": seq,
        "category": category,
        "event": event,
        "subject_id": subject_id,
        "actor_id": actor_id,
        "payload": payload or {},
    }


def chain_hash(prev_hash: str | None, core: dict[str, Any]) -> str:
    """Compute ``entry_hash = sha256(prev_hash || canonical_json(core))``."""
    prefix = prev_hash if prev_hash is not None else GENESIS_PREV_HASH
    return sha256_hex(prefix + canonical_json(core))


__all__ = [
    "GENESIS_PREV_HASH",
    "canonical_json",
    "chain_hash",
    "payload_core",
    "sha256_hex",
]
