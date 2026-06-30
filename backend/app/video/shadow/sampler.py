"""Deterministic shadow sampling — which real requests also get a candidate render.

Shadow mode renders the candidate model for only a *fraction* of real requests so
the eval load (and any funded spend) stays bounded. The decision must be:

* **Deterministic per shot** — the same ``shot_id`` always lands the same way, so a
  retried/duplicated request never double-samples and a replay reproduces exactly.
* **Stable under fraction changes** — raising the fraction only *adds* shots to the
  sample (monotone inclusion), so widening the rollout never drops a shot that was
  already being evaluated. This is the classic "hash the key, keep if below the
  threshold" trick.
* **Salted** — a per-candidate ``salt`` decorrelates the sampled set across
  different candidate evaluations running concurrently, so two candidates don't
  always evaluate the exact same shots.

No global RNG, no clock — pure function of ``(shot_id, fraction, salt)``.
"""

from __future__ import annotations

import hashlib

#: Width of the hash bucket space. ``2**53`` keeps the ratio exactly representable
#: as a Python float (mantissa is 53 bits) so ``bucket / _BUCKETS`` is exact.
_BUCKETS = 1 << 53


def _unit_hash(key: str) -> float:
    """Map an arbitrary string to a stable value in ``[0, 1)``.

    Uses BLAKE2b (in the stdlib, fast, no external dep) over ``key`` and folds the
    top 53 bits to a float. Independent of ``PYTHONHASHSEED`` (unlike ``hash()``),
    so it is reproducible across processes and runs.
    """
    digest = hashlib.blake2b(key.encode("utf-8"), digest_size=8).digest()
    value = int.from_bytes(digest, "big") >> (64 - 53)
    return value / _BUCKETS


class DeterministicSampler:
    """A hash-bucket :class:`~app.video.shadow.seams.Sampler`.

    ``fraction`` is clamped to ``[0, 1]``. ``0.0`` samples nothing (the default —
    shadow mode opt-in); ``1.0`` samples everything (full mirror, for replay/soak).
    """

    def __init__(self, fraction: float, *, salt: str = "shadow") -> None:
        self._fraction = min(1.0, max(0.0, float(fraction)))
        self._salt = salt

    @property
    def fraction(self) -> float:
        """The clamped sampling fraction in ``[0, 1]``."""
        return self._fraction

    def in_sample(self, shot_id: str) -> bool:
        """True iff ``shot_id`` falls in the sampled fraction.

        Exact-boundary semantics: ``0.0`` includes nothing, ``1.0`` includes
        everything, and otherwise a shot is included iff its salted unit-hash is
        strictly below ``fraction`` (monotone in ``fraction``).
        """
        if self._fraction <= 0.0:
            return False
        if self._fraction >= 1.0:
            return True
        return _unit_hash(f"{self._salt}:{shot_id}") < self._fraction


class AlwaysSampler:
    """A :class:`~app.video.shadow.seams.Sampler` that samples every shot.

    Convenience for replay / full-mirror soak tests where every recorded spec must
    run on the candidate.
    """

    def in_sample(self, shot_id: str) -> bool:  # noqa: D102 - trivial
        return True


__all__ = ["AlwaysSampler", "DeterministicSampler"]
