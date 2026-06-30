"""The hash-chain primitive used to verify crypto-erasure integrity.

The append-only stores (the domain event store and the compliance audit log)
commit each record to a tamper-evident chain::

    entry_hash = sha256( prev_hash || canonical_json(core) )

Right-to-erasure cannot *delete* an entry in such a store without breaking every
subsequent ``entry_hash``. Instead it **redacts**: the personal fields in an
entry's payload are replaced with a redaction marker and the entry's hash is
*re-derived* over the redacted content, with every following entry re-chained so
the whole chain re-verifies. This module is the pure primitive that lets the
privacy subsystem (a) re-derive a redacted entry's hash and (b) verify a chain
end-to-end after redaction — independent of where the chain physically lives.

Pure (no DB, no I/O); a local copy of the same construction
:mod:`app.compliance.ledger.chain` uses, kept dependency-free so the audit-log
sibling can later satisfy the :class:`~app.privacy.protocols.AuditRedactor`
protocol without either package importing the other.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

#: The sentinel ``prev_hash`` of the very first entry in the chain (genesis).
GENESIS_PREV_HASH = "0" * 64


def canonical_json(value: Any) -> str:
    """Serialise ``value`` deterministically (sorted keys, compact, UTF-8 safe)."""
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


def chain_hash(prev_hash: str | None, core: Any) -> str:
    """Compute ``entry_hash = sha256(prev_hash || canonical_json(core))``."""
    prefix = prev_hash if prev_hash is not None else GENESIS_PREV_HASH
    return sha256_hex(prefix + canonical_json(core))


__all__ = [
    "GENESIS_PREV_HASH",
    "canonical_json",
    "chain_hash",
    "sha256_hex",
]
