"""Deterministic bucketing — the heart of sticky rollouts and reproducible A/Bs.

Every assignment decision (is this unit inside a 30% rollout? which experiment
arm does it land in?) reduces to a single primitive: a *stable* hash of the
``unit`` (a user id, tenant id, session id, …) under a ``salt``, projected onto
``[0, TOTAL_BASIS_POINTS)``. The same ``(unit, salt)`` pair always yields the
same bucket, in every process, on every machine, forever — which is what makes a
rollout sticky (a user does not flip in and out as you scale 10% → 20%) and an
experiment reproducible (re-running the §13 harness assigns the same arms).

We work in **basis points** (1 bp = 0.01%) so a rollout can be expressed at
0.01% resolution and ``TOTAL_BASIS_POINTS`` (= 10_000) is exact in integer math.

Salt independence: each flag rollout uses its own salt (``flag_key`` by default)
and each experiment uses its own salt, so a unit's rollout bucket and its
experiment bucket are statistically independent — no carry-over bias where the
same users always end up "early" in every rollout *and* in arm A of every test.
"""

from __future__ import annotations

import hashlib

#: Bucketing granularity. A unit hashes into ``[0, TOTAL_BASIS_POINTS)``; a
#: rollout of ``p`` percent admits a unit iff its bucket ``< p * 100`` bp.
TOTAL_BASIS_POINTS = 10_000

#: 2**32, the size of the uint32 window we read from the digest before scaling.
_UINT32 = 1 << 32


def _digest_uint32(text: str) -> int:
    """Return the leading 32 bits of ``SHA-256(text)`` as an unsigned int.

    SHA-256 (not Python's salted ``hash()``) so the value is stable across
    processes and interpreter restarts; 32 bits is ample entropy for 1bp
    buckets and keeps the arithmetic in machine-int range.
    """
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big")


def bucket_bp(unit: str, salt: str) -> int:
    """Map ``unit`` under ``salt`` to a basis-point bucket in ``[0, 10000)``.

    Implemented as ``floor(uint32 / 2**32 * 10000)`` — a uniform projection of
    the 32-bit hash window onto the 10_000-wide basis-point line. Deterministic
    and uniform: over many units the buckets are ~evenly spread, so a *p*%
    rollout admits ~*p*% of the population.
    """
    return (_digest_uint32(f"{salt}:{unit}") * TOTAL_BASIS_POINTS) // _UINT32


def bucket_fraction(unit: str, salt: str) -> float:
    """The bucket as a fraction in ``[0.0, 1.0)`` (convenience over :func:`bucket_bp`)."""
    return bucket_bp(unit, salt) / TOTAL_BASIS_POINTS


def in_rollout(unit: str, salt: str, percent: float) -> bool:
    """Whether ``unit`` is inside a ``percent`` rollout (0..100) under ``salt``.

    ``percent <= 0`` admits no one; ``percent >= 100`` admits everyone. The
    comparison is ``bucket < percent * 100`` in basis points so growing the
    rollout only ever *adds* units (stickiness): everyone admitted at 10% is
    still admitted at 20%.
    """
    if percent <= 0:
        return False
    if percent >= 100:
        return True
    threshold_bp = round(percent * 100)
    return bucket_bp(unit, salt) < threshold_bp


def weighted_index(unit: str, salt: str, weights: tuple[int, ...]) -> int:
    """Pick an index into ``weights`` (basis points summing to 10_000) for ``unit``.

    Lays the weighted variations end-to-end on the ``[0, 10000)`` line and
    returns the index whose interval contains the unit's bucket. Deterministic
    and proportional: a variation with weight ``w`` bp receives ~``w/10000`` of
    units. The final interval absorbs any rounding slack so a unit at the very
    top of the line always lands somewhere.
    """
    if not weights:
        raise ValueError("weights must be non-empty")
    if any(w < 0 for w in weights):
        raise ValueError("weights must be non-negative")
    total = sum(weights)
    if total <= 0:
        raise ValueError("weights must sum to a positive value")
    # Scale the unit's [0,10000) bucket into [0,total) so partial-sum weights
    # (which may not total 10000) still partition the space exactly.
    scaled = (bucket_bp(unit, salt) * total) // TOTAL_BASIS_POINTS
    cursor = 0
    for index, weight in enumerate(weights):
        cursor += weight
        if scaled < cursor:
            return index
    return len(weights) - 1  # pragma: no cover - rounding backstop


__all__ = [
    "TOTAL_BASIS_POINTS",
    "bucket_bp",
    "bucket_fraction",
    "in_rollout",
    "weighted_index",
]
