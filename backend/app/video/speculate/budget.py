"""In-memory speculative-spend ledger + cache seam defaults (kinora.md §11).

Concrete, dependency-free implementations of the :mod:`app.video.speculate.protocols`
seams so the engine runs standalone (tests, local) without wiring the real billing
ledger or cache. A composition root may substitute production adapters that satisfy
the same protocols.

:class:`InMemorySpeculativeBudget` is a hard-capped reserve/release/settle ledger:

    reserve  → at-risk dollars (refundable while unstarted)
    settle   → at-risk → realised (render started/landed; no longer refundable)
    release  → at-risk → returned to the pool (cancellation refund)

The invariant ``reserved + spent <= ceiling`` holds at all times, so the engine
**can never spend past the speculative budget** — the planner asks ``remaining_usd``
and the reserve call refuses anything that would breach the cap.
"""

from __future__ import annotations


class InMemorySpeculativeBudget:
    """A hard-capped speculative-spend ledger (thread-unsafe; one per session loop)."""

    def __init__(self, ceiling_usd: float) -> None:
        if ceiling_usd < 0.0:
            raise ValueError("speculative budget ceiling must be >= 0")
        self._ceiling = float(ceiling_usd)
        self._reserved = 0.0
        self._spent = 0.0

    # -- SpeculativeBudgetProtocol ---------------------------------------- #

    def remaining_usd(self) -> float:
        return max(0.0, round(self._ceiling - self._reserved - self._spent, 6))

    def reserve(self, usd: float) -> bool:
        amount = max(0.0, usd)
        if amount > self.remaining_usd() + 1e-9:
            return False
        self._reserved = round(self._reserved + amount, 6)
        return True

    def release(self, usd: float) -> None:
        self._reserved = max(0.0, round(self._reserved - max(0.0, usd), 6))

    def settle(self, usd: float) -> None:
        amount = min(self._reserved, max(0.0, usd))
        self._reserved = max(0.0, round(self._reserved - amount, 6))
        self._spent = round(self._spent + amount, 6)

    # -- introspection ---------------------------------------------------- #

    @property
    def ceiling_usd(self) -> float:
        return self._ceiling

    @property
    def reserved_usd(self) -> float:
        return self._reserved

    @property
    def spent_usd(self) -> float:
        return self._spent

    @property
    def utilisation(self) -> float:
        """Fraction of the ceiling currently committed (reserved + spent), 0..1."""
        if self._ceiling <= 1e-9:
            return 1.0
        return min(1.0, round((self._reserved + self._spent) / self._ceiling, 6))


class NullCache:
    """A cache seam that never has and never salvages (the conservative default)."""

    def is_salvageable(self, shot_key: str) -> bool:
        return False

    def has(self, shot_key: str) -> bool:
        return False


class SetCache:
    """A trivial in-memory cache seam backed by two key sets (for tests/local).

    ``present`` keys answer :meth:`has` (a free hit); ``salvageable`` keys answer
    :meth:`is_salvageable` (worth keeping on cancellation).
    """

    def __init__(
        self,
        present: set[str] | None = None,
        salvageable: set[str] | None = None,
    ) -> None:
        self._present = set(present or ())
        self._salvageable = set(salvageable or ())

    def is_salvageable(self, shot_key: str) -> bool:
        return shot_key in self._salvageable or shot_key in self._present

    def has(self, shot_key: str) -> bool:
        return shot_key in self._present

    def add(self, shot_key: str, *, salvageable: bool = True) -> None:
        self._present.add(shot_key)
        if salvageable:
            self._salvageable.add(shot_key)


__all__ = [
    "InMemorySpeculativeBudget",
    "NullCache",
    "SetCache",
]
