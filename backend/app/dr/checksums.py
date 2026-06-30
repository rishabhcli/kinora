"""Canonical serialisation + SHA-256 integrity checksums.

A backup segment (canon, the event slice, checkpoints, read models, the asset
manifest) is hashed so a single flipped byte — bit-rot on disk, a truncated
upload, a malicious edit — is caught on restore before it can corrupt the
rebuilt state. Two properties make the digest trustworthy and reproducible:

* **Canonical encoding.** JSON is serialised with sorted keys, no insignificant
  whitespace, and ``ensure_ascii`` so the *same logical value* always hashes to
  the same digest regardless of dict insertion order or the producer's platform.
* **Determinism.** No clock, no RNG, no environment input — the digest is a pure
  function of the value. A test can therefore assert an exact digest and a
  corruption test can flip one field and assert the digest changes.

The :class:`Checksum` value object pairs the algorithm with the hex digest so a
future migration to a stronger hash is a data change, not a format change.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

#: The only algorithm currently emitted. Stored alongside each digest so a
#: verifier can refuse an unknown algorithm rather than silently mis-compare.
ALGORITHM = "sha256"


def canonical_bytes(value: Any) -> bytes:
    """Encode ``value`` to canonical UTF-8 JSON bytes (the hashing pre-image).

    Sorted keys + compact separators make the encoding independent of dict
    ordering, so two logically-equal payloads hash identically. ``default=str``
    is a safety net for stray non-JSON scalars (e.g. a ``Decimal``); the wire
    models only ever carry JSON-native types, so it is rarely exercised.
    """
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        default=str,
    ).encode("utf-8")


def digest(value: Any) -> str:
    """Return the lowercase hex SHA-256 of ``value``'s canonical encoding."""
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def digest_bytes(raw: bytes) -> str:
    """Return the lowercase hex SHA-256 of raw ``bytes`` (for opaque blobs)."""
    return hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True, slots=True)
class Checksum:
    """An ``(algorithm, hex-digest)`` pair identifying a segment's bytes."""

    algorithm: str
    value: str

    @classmethod
    def of(cls, value: Any) -> Checksum:
        """Compute the checksum of a JSON-able ``value``."""
        return cls(algorithm=ALGORITHM, value=digest(value))

    @classmethod
    def of_bytes(cls, raw: bytes) -> Checksum:
        """Compute the checksum of an opaque byte blob."""
        return cls(algorithm=ALGORITHM, value=digest_bytes(raw))

    def matches(self, value: Any) -> bool:
        """Return whether ``value`` re-hashes to this checksum (algorithm-aware)."""
        if self.algorithm != ALGORITHM:
            return False
        return digest(value) == self.value

    def as_str(self) -> str:
        """A compact ``"algorithm:hex"`` string for manifests/logs."""
        return f"{self.algorithm}:{self.value}"


def combine(*checksums: Checksum) -> Checksum:
    """Fold child checksums into one parent digest (a Merkle-style roll-up).

    Used to give a whole snapshot a single ``content_hash`` over its segments:
    the order-independence comes from sorting the children's string forms before
    hashing, so the roll-up is stable regardless of segment capture order.
    """
    parts = sorted(c.as_str() for c in checksums)
    return Checksum(algorithm=ALGORITHM, value=digest(parts))


__all__ = [
    "ALGORITHM",
    "Checksum",
    "canonical_bytes",
    "combine",
    "digest",
    "digest_bytes",
]
