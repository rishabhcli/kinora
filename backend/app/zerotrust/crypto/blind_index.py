"""Blind indexes — keyed, irreversible search tokens for encrypted columns.

A blind index is the standard way to query encrypted data *without* a decryption
oracle. For each searchable column you store, alongside the ciphertext, a keyed
HMAC of a *normalised* form of the plaintext. The index is:

* **keyed** — without the index key (derived from the same hierarchy as the
  encryption keys) you cannot compute a token, so the column is useless to a
  thief who only has the table;
* **irreversible** — HMAC-SHA256 is one-way, so the index never reveals the
  plaintext, only that two rows share an indexed transform of it.

Three index flavours are provided:

* :func:`equality_index` — token of the whole normalised value; powers
  ``WHERE col_bidx = ?`` exact match. (Deterministic encryption can do this too;
  a blind index is preferable when you do *not* want the ciphertext itself to be
  deterministic — e.g. you want randomised storage but still searchable.)
* :func:`prefix_indexes` — one token per prefix length up to a cap; powers
  ``LIKE 'foo%'`` as ``WHERE prefix_bidx IN (...)`` after tokenising the probe.
* :func:`range_buckets` — coarse, order-preserving *bucket* tokens for numeric
  or date ranges. Buckets trade exactness for privacy: a range query expands to
  the bucket tokens it spans plus a residual decrypt-and-filter on the boundary
  buckets (the boundary filter keeps results exact while the buckets keep the
  bulk scan keyed and value-hiding).

Truncation: tokens are truncated to :data:`TOKEN_BYTES` (default 16). 128 bits of
HMAC output is collision-resistant for index purposes and halves storage; the
full digest is never needed because a blind index only ever tests equality of
tokens, and a deliberate collision requires the secret key.
"""

from __future__ import annotations

import hashlib
import hmac

#: Truncated token width. 16 bytes = 128 bits: ample collision resistance for an
#: index, and compact in the row.
TOKEN_BYTES = 16

#: Domain-separation tags so an equality token can never equal a prefix or bucket
#: token of the same value (which would leak structure across index types).
_EQUALITY_TAG = b"\x01eq"
_PREFIX_TAG = b"\x02px"
_BUCKET_TAG = b"\x03bk"


def _token(key: bytes, tag: bytes, payload: bytes) -> bytes:
    """Keyed, domain-separated, truncated HMAC token."""
    return hmac.new(key, tag + b"\x00" + payload, hashlib.sha256).digest()[:TOKEN_BYTES]


def equality_index(key: bytes, value: bytes) -> bytes:
    """Token for an exact-match index on the (already normalised) ``value``."""
    return _token(key, _EQUALITY_TAG, value)


def prefix_indexes(
    key: bytes, value: bytes, *, max_len: int = 16, min_len: int = 1
) -> list[bytes]:
    """Tokens for every prefix of ``value`` from ``min_len`` to ``max_len`` chars.

    Stored as a side table (one row per token) so a ``LIKE 'abc%'`` query becomes
    an indexed lookup of the single token for ``'abc'``. Capping ``max_len``
    bounds write amplification; prefixes longer than the cap fall back to a
    decrypt-and-filter on the candidate set the cap-length prefix returns.
    """
    if min_len < 1:
        raise ValueError("min_len must be >= 1")
    text = value.decode("utf-8", errors="surrogatepass")
    upper = min(len(text), max_len)
    return [
        _token(key, _PREFIX_TAG, text[:length].encode("utf-8", errors="surrogatepass"))
        for length in range(min_len, upper + 1)
    ]


def prefix_query_token(key: bytes, prefix: bytes, *, max_len: int = 16) -> bytes:
    """The single token to match for a ``LIKE 'prefix%'`` probe.

    Mirrors :func:`prefix_indexes`' tokenisation for one prefix (clamped to the
    same ``max_len`` so the probe lines up with the stored tokens).
    """
    text = prefix.decode("utf-8", errors="surrogatepass")[:max_len]
    return _token(key, _PREFIX_TAG, text.encode("utf-8", errors="surrogatepass"))


def range_buckets(key: bytes, value: int, *, bucket_size: int) -> bytes:
    """Token for the coarse bucket ``value`` falls into (for range queries).

    ``floor(value / bucket_size)`` is HMACed, so contiguous values collapse to one
    keyed token. A range query computes the bucket tokens spanning ``[lo, hi]``
    (see :func:`buckets_for_range`) and decrypts only the boundary buckets to
    re-filter exactly. ``bucket_size`` is the privacy/precision dial.
    """
    if bucket_size <= 0:
        raise ValueError("bucket_size must be positive")
    bucket = value // bucket_size
    return _token(key, _BUCKET_TAG, str(bucket).encode("ascii"))


def buckets_for_range(
    key: bytes, lo: int, hi: int, *, bucket_size: int, max_buckets: int = 4096
) -> list[bytes]:
    """The bucket tokens covering the closed integer range ``[lo, hi]``.

    Raises:
        ValueError: if the range spans more than ``max_buckets`` buckets (a guard
            against an unbounded ``IN (...)`` list — widen ``bucket_size`` instead).
    """
    if bucket_size <= 0:
        raise ValueError("bucket_size must be positive")
    if hi < lo:
        return []
    first, last = lo // bucket_size, hi // bucket_size
    if last - first + 1 > max_buckets:
        raise ValueError(
            f"range [{lo},{hi}] spans {last - first + 1} buckets (> {max_buckets}); "
            "use a larger bucket_size"
        )
    return [
        _token(key, _BUCKET_TAG, str(b).encode("ascii")) for b in range(first, last + 1)
    ]


__all__ = [
    "TOKEN_BYTES",
    "buckets_for_range",
    "equality_index",
    "prefix_indexes",
    "prefix_query_token",
    "range_buckets",
]
