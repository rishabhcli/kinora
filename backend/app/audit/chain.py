"""The hash-chain + Merkle primitives backing the tamper-evident audit log.

Two layers of tamper-evidence:

**1. The per-entry hash chain.** Each entry's ``entry_hash`` commits to the
previous entry's hash plus a *canonicalised* projection of its own logical
content::

    entry_hash = sha256( prev_hash || canonical_json(record_core) )

Because every entry's hash feeds the next, editing, inserting, or deleting any
entry breaks every hash after it — :func:`recompute_chain` re-derives the whole
chain and the first divergence pinpoints the tamper.

**2. Optional Merkle checkpoints.** Every *N* entries the log seals a checkpoint:
a Merkle root over that segment's entry hashes. A checkpoint is a compact
commitment a third party can countersign / publish, so even an attacker who can
rewrite the whole append-only table cannot forge a segment that matches a
previously-published root. :func:`merkle_root` is the duplicate-last-leaf binary
Merkle construction; :func:`merkle_proof` / :func:`verify_merkle_proof` give an
O(log n) inclusion proof for any single entry.

The redaction story (see :mod:`app.audit.redaction`) depends on the hash being
computed over the *un-redacted* core: the stored payload may later be scrubbed
of PII, but the chain still verifies because the hash committed to the original
content, recorded once at append time. This module therefore never redacts — it
hashes exactly the core it is given.

Pure module: no DB, no I/O, no clock. Trivially unit-testable.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

#: The sentinel ``prev_hash`` of the very first entry in the chain (genesis).
GENESIS_PREV_HASH = "0" * 64

#: Domain-separation tags so a leaf hash can never be confused with an interior
#: Merkle node (a classic second-preimage defence for Merkle trees).
_LEAF_TAG = b"\x00"
_NODE_TAG = b"\x01"


def sha256_hex(data: str) -> str:
    """Return the hex SHA-256 of a UTF-8 string."""
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def canonical_json(value: Any) -> str:
    """Serialise ``value`` deterministically (sorted keys, compact, UTF-8 safe).

    ``default=str`` lets datetimes / enums fall back to their string form so the
    projection never raises on a value the JSON encoder cannot natively handle.
    The result is byte-identical regardless of input dict ordering, which is
    what makes the hash reproducible by an independent verifier.
    """
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )


def record_core(
    *,
    seq: int,
    event_id: str,
    occurred_at: str,
    category: str,
    action: str,
    severity: str,
    actor_kind: str,
    actor_id: str,
    target_type: str | None,
    target_id: str | None,
    correlation_id: str | None,
    trace_id: str | None,
    reason: str | None,
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
    payload: dict[str, Any] | None,
) -> dict[str, Any]:
    """The canonical projection of an entry's content the hash commits to.

    Includes *everything* that is logically part of the event — including the
    ``before``/``after`` snapshots — so any retroactive edit to any field is
    detectable. Deliberately excludes the storage row's ``id`` and ``prev_hash``
    (chained separately) and any redaction bookkeeping (which is applied *after*
    hashing). ``occurred_at`` is the event's own logical timestamp, not the
    DB-assigned ``created_at``, so the chain is reproducible from logical fields.
    """
    return {
        "seq": seq,
        "event_id": event_id,
        "occurred_at": occurred_at,
        "category": category,
        "action": action,
        "severity": severity,
        "actor_kind": actor_kind,
        "actor_id": actor_id,
        "target_type": target_type,
        "target_id": target_id,
        "correlation_id": correlation_id,
        "trace_id": trace_id,
        "reason": reason,
        "before": before,
        "after": after,
        "payload": payload or {},
    }


def chain_hash(prev_hash: str | None, core: dict[str, Any]) -> str:
    """Compute ``entry_hash = sha256(prev_hash || canonical_json(core))``."""
    prefix = prev_hash if prev_hash is not None else GENESIS_PREV_HASH
    return sha256_hex(prefix + canonical_json(core))


# --------------------------------------------------------------------------- #
# Chain verification
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ChainCheck:
    """The result of re-hashing a hash-chain.

    ``ok`` is True iff every entry's ``seq`` is contiguous from 1, every
    ``prev_hash`` matches the preceding entry's ``entry_hash``, and every
    ``entry_hash`` recomputes from its core. ``broken_at_seq`` is the first
    offending ``seq`` (None when intact); ``reason`` is a human-readable cause.
    """

    ok: bool
    entries: int
    broken_at_seq: int | None = None
    reason: str | None = None


@dataclass(frozen=True)
class _ChainItem:
    """A minimal verifiable view of one stored entry (decoupled from the ORM)."""

    seq: int
    prev_hash: str
    entry_hash: str
    core: dict[str, Any]


def recompute_chain(items: list[_ChainItem]) -> ChainCheck:
    """Re-derive the whole chain and report the first tamper (if any).

    Detects all three tamper modes:

    * **edit** — a changed field makes ``recompute != stored entry_hash``;
    * **delete** — a removed entry leaves a ``seq`` gap (or a ``prev_hash`` that
      points at a now-missing predecessor);
    * **insert** — a forged entry either duplicates a ``seq`` (gap check) or has a
      ``prev_hash`` that does not match the real predecessor.
    """
    prev_hash: str | None = None
    for index, item in enumerate(items):
        expected_seq = index + 1
        if item.seq != expected_seq:
            return ChainCheck(
                ok=False,
                entries=len(items),
                broken_at_seq=item.seq,
                reason=f"sequence gap: expected {expected_seq}, got {item.seq}",
            )
        expected_prev = prev_hash if prev_hash is not None else GENESIS_PREV_HASH
        if (item.prev_hash or GENESIS_PREV_HASH) != expected_prev:
            return ChainCheck(
                ok=False,
                entries=len(items),
                broken_at_seq=item.seq,
                reason="prev_hash does not match the preceding entry",
            )
        recomputed = chain_hash(item.prev_hash, item.core)
        if recomputed != item.entry_hash:
            return ChainCheck(
                ok=False,
                entries=len(items),
                broken_at_seq=item.seq,
                reason="entry_hash does not match recomputed core",
            )
        prev_hash = item.entry_hash
    return ChainCheck(ok=True, entries=len(items))


# --------------------------------------------------------------------------- #
# Merkle checkpoints
# --------------------------------------------------------------------------- #


def _leaf_hash(value: str) -> str:
    """Tagged leaf hash for a single entry-hash hex string."""
    return hashlib.sha256(_LEAF_TAG + value.encode("utf-8")).hexdigest()


def _node_hash(left: str, right: str) -> str:
    """Tagged interior-node hash combining two child hex digests."""
    return hashlib.sha256(_NODE_TAG + bytes.fromhex(left) + bytes.fromhex(right)).hexdigest()


def merkle_root(leaves: list[str]) -> str:
    """Merkle root over ``leaves`` (entry-hash hex strings).

    Uses the duplicate-last-leaf convention for odd levels (Bitcoin-style). An
    empty input yields the all-zeroes root so a checkpoint over no entries is
    still well-defined.
    """
    if not leaves:
        return GENESIS_PREV_HASH
    level = [_leaf_hash(leaf) for leaf in leaves]
    while len(level) > 1:
        if len(level) % 2 == 1:
            level.append(level[-1])  # duplicate the last node
        level = [_node_hash(level[i], level[i + 1]) for i in range(0, len(level), 2)]
    return level[0]


@dataclass(frozen=True)
class MerkleStep:
    """One sibling on the path from a leaf to the root.

    ``sibling`` is the co-node's digest; ``on_right`` says whether the sibling
    sits to the *right* of the running hash (so the verifier knows the
    concatenation order).
    """

    sibling: str
    on_right: bool


def merkle_proof(leaves: list[str], index: int) -> list[MerkleStep]:
    """An O(log n) inclusion proof for ``leaves[index]``.

    The returned steps, folded into the leaf hash, reproduce :func:`merkle_root`.
    """
    if not 0 <= index < len(leaves):
        raise IndexError(f"leaf index {index} out of range for {len(leaves)} leaves")
    level = [_leaf_hash(leaf) for leaf in leaves]
    path: list[MerkleStep] = []
    pos = index
    while len(level) > 1:
        if len(level) % 2 == 1:
            level.append(level[-1])
        if pos % 2 == 0:  # we are the left child; sibling is on the right
            path.append(MerkleStep(sibling=level[pos + 1], on_right=True))
        else:  # we are the right child; sibling is on the left
            path.append(MerkleStep(sibling=level[pos - 1], on_right=False))
        level = [_node_hash(level[i], level[i + 1]) for i in range(0, len(level), 2)]
        pos //= 2
    return path


def verify_merkle_proof(leaf: str, proof: list[MerkleStep], root: str) -> bool:
    """True iff folding ``proof`` into ``leaf`` reproduces ``root``."""
    running = _leaf_hash(leaf)
    for step in proof:
        if step.on_right:
            running = _node_hash(running, step.sibling)
        else:
            running = _node_hash(step.sibling, running)
    return running == root


__all__ = [
    "GENESIS_PREV_HASH",
    "ChainCheck",
    "MerkleStep",
    "_ChainItem",
    "canonical_json",
    "chain_hash",
    "merkle_proof",
    "merkle_root",
    "recompute_chain",
    "record_core",
    "sha256_hex",
    "verify_merkle_proof",
]
