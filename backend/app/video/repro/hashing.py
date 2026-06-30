"""Canonical, collision-resistant digest primitives for render provenance.

Every reproducibility artifact in :mod:`app.video.repro` keys off a *stable
digest* of structured inputs. The whole subsystem's promise — "the same logical
shot re-renders identically, and we can prove why two clips differ" — rests on
those digests being:

* **deterministic** — identical input → identical digest, on any machine, in any
  process, across Python runs (so a fingerprint computed during ingest matches
  one recomputed months later during a replay);
* **order-insensitive where order is not meaning** — a reference set is a *set*,
  so ``[a, b]`` and ``[b, a]`` must digest identically (the §8.7 cache would
  otherwise miss on a trivially-reordered list and re-spend video-seconds);
* **boundary-unambiguous** — ``"ab" + "c"`` must not collide with ``"a" + "bc"``;
* **type-faithful** — the integer ``1``, the string ``"1"``, the float ``1.0``,
  and the bool ``True`` are distinct inputs and must not alias.

This module is intentionally separate from :mod:`app.db.hashing` (which owns the
narrow §8.7 ``shot_hash`` cache key): the fingerprint captures *far more* than
the six cache-key components, so it needs a general canonicaliser for arbitrary
nested JSON-like structures. It reuses the same unit-separator discipline and
SHA-256 (a wider digest than the cache key's SHA-1, because a fingerprint is a
provenance record, not just a cache slot, and we never want a provenance
collision).

The functions here are pure and free of I/O.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

#: Unit separator — cannot appear in textual inputs, so joined components keep a
#: hard boundary (mirrors :mod:`app.db.hashing`).
_SEP = "\x1f"

#: Algorithm name carried alongside every digest so a future migration to a
#: wider hash stays self-describing in persisted manifests.
DIGEST_ALGORITHM = "sha256"

#: A digest of the canonical empty value — handy sentinel for "nothing here".
EMPTY_DIGEST_INPUT = "\x00empty"


def _typed_token(value: Any) -> Any:
    """Render *value* into a JSON-serialisable form that is **type-faithful**.

    ``json.dumps`` already distinguishes ``1`` / ``"1"`` / ``1.0`` / ``true`` in
    its output text, so for the scalar/JSON case we can lean on it directly. The
    one place we must intervene is containers: a Python ``set``/``frozenset`` is
    unordered, but JSON has no set type, so we sort its *already-canonicalised*
    members to a list with a leading set-marker. Tuples are encoded distinctly
    from lists so a positional tuple never aliases a list.
    """
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, bytes):
        # Hash bytes rather than embed them: keeps manifests small and JSON-safe.
        return {"__bytes_sha256__": hashlib.sha256(value).hexdigest()}
    if isinstance(value, dict):
        # Keys are coerced to str for JSON; recurse into values.
        return {str(k): _typed_token(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        marker = "__tuple__" if isinstance(value, tuple) else "__list__"
        return {marker: [_typed_token(v) for v in value]}
    if isinstance(value, (set, frozenset)):
        # Canonicalise each member, then sort by its canonical text so member
        # order cannot change the digest.
        members = sorted(canonical_text(m) for m in value)
        return {"__set__": members}
    # Fallback: stable string form (e.g. enums whose ``str`` is meaningful).
    return {"__repr__": f"{type(value).__name__}:{value}"}


def canonical_text(value: Any) -> str:
    """Return the deterministic canonical JSON text for *value*.

    Dict keys are sorted; sets are order-normalised; container types are tagged
    so they never alias one another. The output is stable across processes
    (``sort_keys=True`` + no whitespace + ``ensure_ascii=False`` for a faithful
    unicode round-trip).
    """
    return json.dumps(
        _typed_token(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def digest(value: Any) -> str:
    """SHA-256 hex digest of *value*'s canonical text. Deterministic and pure."""
    return hashlib.sha256(canonical_text(value).encode("utf-8")).hexdigest()


def digest_fields(**fields: Any) -> str:
    """Digest a set of named fields as an order-insensitive mapping.

    Convenience for the common "hash these labelled components" case; equivalent
    to :func:`digest` of the ``fields`` dict, but reads as a manifest at the call
    site.
    """
    return digest(fields)


def join_digest(*parts: str) -> str:
    """Digest an *ordered* sequence of already-textual parts (boundary-safe).

    Mirrors the §8.7 cache-key construction: parts are joined with the unit
    separator that cannot appear inside them, so ``("ab", "c")`` and
    ``("a", "bc")`` never collide. Use this when the *order* of parts is itself
    meaningful (e.g. a seed-path), unlike :func:`digest_fields`.
    """
    payload = _SEP.join(parts)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def short(d: str, *, length: int = 12) -> str:
    """A short prefix of a hex digest for human-facing labels/logs (never a key)."""
    return d[:length]


__all__ = [
    "DIGEST_ALGORITHM",
    "EMPTY_DIGEST_INPUT",
    "canonical_text",
    "digest",
    "digest_fields",
    "join_digest",
    "short",
]
