"""Deterministic weighted provider selection (canary / A-B routing).

The registry needs to send, say, 5% of traffic to a canary model and the rest to
the incumbent. This module is the pure math for that: a stable mapping from a
*routing key* (a shot id, a session id — anything stable for the unit of traffic
you want to keep on one provider) to one of N weighted candidates.

Two design rules:

* **Deterministic.** No global RNG. The same routing key always lands on the
  same provider for a given candidate set, so a render that retries doesn't
  flip providers mid-shot, and a statistical test can assert the *distribution*
  by sweeping many keys. We hash ``(salt, key)`` to a uniform ``[0, 1)`` and
  walk the cumulative-weight ladder — the classic weighted-reservoir-by-CDF.
* **Weights are relative.** They need not sum to 1; we normalize by the total.
  A zero-weight candidate is never selected. An empty/all-zero candidate set
  yields ``None`` (the caller decides what to do — there is no traffic to route).

The hash is :func:`hashlib.blake2b` over ``f"{salt}:{key}"`` — fast, stable
across processes and Python versions (unlike ``hash()``), and uniform enough
that a few thousand keys reproduce the configured split to within a percent.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

#: Default salt for the routing hash. Override per-experiment to re-shuffle the
#: key→provider assignment without changing weights (a fresh A/B bucketing).
DEFAULT_SALT = "kinora.video.registry.v1"


def _uniform_unit(key: str, *, salt: str) -> float:
    """Map ``key`` to a stable, uniform float in ``[0.0, 1.0)``.

    blake2b is process- and version-stable (Python's built-in ``hash()`` is
    salted per process), so the same key reproduces the same bucket everywhere.
    We take the leading 8 bytes as a big-endian unsigned int and divide by 2**64.
    """
    digest = hashlib.blake2b(f"{salt}:{key}".encode(), digest_size=8).digest()
    return int.from_bytes(digest, "big") / 2**64


@dataclass(frozen=True, slots=True)
class WeightedCandidate:
    """One pickable option: an opaque ``id`` and a non-negative ``weight``."""

    id: str
    weight: float

    def __post_init__(self) -> None:
        if self.weight < 0:
            raise ValueError(f"weight must be >= 0, got {self.weight} for {self.id!r}")


def pick_weighted(
    candidates: Sequence[WeightedCandidate],
    routing_key: str,
    *,
    salt: str = DEFAULT_SALT,
) -> str | None:
    """Deterministically select one candidate id by weight for ``routing_key``.

    Walks the cumulative-weight CDF at the key's uniform hash point. Stable:
    same ``(candidates, routing_key, salt)`` ⇒ same result. Returns ``None`` when
    there is nothing to route (no candidates, or every weight is zero).

    The candidate order does **not** affect the *distribution* (only which
    concrete id wins a given bucket), so callers may pass any stable order.
    """
    total = sum(max(0.0, c.weight) for c in candidates)
    if total <= 0.0:
        return None
    point = _uniform_unit(routing_key, salt=salt) * total
    cumulative = 0.0
    last_positive: str | None = None
    for candidate in candidates:
        w = max(0.0, candidate.weight)
        if w <= 0.0:
            continue
        last_positive = candidate.id
        cumulative += w
        if point < cumulative:
            return candidate.id
    # Floating-point guard: ``point`` can equal ``total`` only via rounding; fall
    # back to the last positively-weighted candidate so we never return None here.
    return last_positive


def expected_distribution(
    candidates: Sequence[WeightedCandidate],
) -> Mapping[str, float]:
    """The ideal traffic share per candidate id (weights normalized to 1.0).

    A convenience for the introspection API and tests: ``{id: weight/total}``
    over positive-weight candidates; ``{}`` when there is nothing to route. Ids
    that repeat are summed.
    """
    positive = {c.id: max(0.0, c.weight) for c in candidates}
    total = sum(positive.values())
    if total <= 0.0:
        return {}
    return {cid: w / total for cid, w in positive.items() if w > 0.0}


__all__ = [
    "DEFAULT_SALT",
    "WeightedCandidate",
    "expected_distribution",
    "pick_weighted",
]
