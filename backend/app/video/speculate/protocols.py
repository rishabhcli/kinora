"""Injectable seams for the speculative pre-generation engine (kinora.md §4.4/§4.6).

The engine is **pure policy over four injectable seams** so its tests need no
infra, no network, and no clock-of-the-wall:

* :class:`Clock` — a virtual monotonic clock (``float`` seconds). Tests drive it
  by hand; production wires :func:`time.monotonic`.
* :class:`CostModel` — per-model cost/latency oracle. Given a model id and a
  duration, it answers "what does this speculative second cost, and how long
  until it lands?". This is the *only* thing that knows provider economics, so
  the planner stays a pure portfolio optimiser.
* :class:`CacheLookup` — a salvage oracle. When a speculation is cancelled, we
  ask the cache whether the partial/finished asset is reusable later (a backward
  seek that lands inside an already-buffered span is a *hit*, not waste, §4.8).
* :class:`SpeculativeBudget` — the spend ledger seam. The engine *reserves*
  speculative video-seconds against it before launching and *releases* them on
  cancellation; it never spends past :meth:`SpeculativeBudget.remaining_s`.

This round is standalone: it deliberately does **not** import any
scheduler/cost/cache/quality package — it defines the *minimal* contract it needs
and lets a composition root adapt the real services onto it. Every method here is
sync and side-effect-light on purpose (the engine awaits nothing in its hot path);
the real adapters can be thin wrappers over async services that pre-resolve state.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

#: A virtual monotonic clock: returns seconds as a float. Never wall-clock — the
#: engine only ever measures *durations*, so any monotonic source is valid.
Clock = "Clock"  # documentary alias; the real type is the Protocol below.


@runtime_checkable
class CostModelProtocol(Protocol):
    """Per-model cost/latency oracle (the only thing that knows provider economics).

    A speculative shot is rendered at some model id (a cheap turbo id for a
    low-probability guess, a premium id reserved for committed/high-probability
    shots). The cost model answers, for a candidate (model_id, video_seconds):

    * :meth:`cost_usd` — the dollar cost of rendering it (what we put at risk);
    * :meth:`latency_s` — how long until the asset lands (so the planner can
      reject a speculation that cannot finish before the reader arrives);
    * :meth:`quality` — a 0..1 fidelity score (premium id > turbo id), used to
      break ties toward better output when EV is equal.
    """

    def cost_usd(self, model_id: str, video_seconds: float) -> float:
        """Dollar cost of rendering ``video_seconds`` at ``model_id``."""
        ...

    def latency_s(self, model_id: str, video_seconds: float) -> float:
        """Wall seconds until the rendered asset is ready at ``model_id``."""
        ...

    def quality(self, model_id: str) -> float:
        """0..1 fidelity score for ``model_id`` (higher = better)."""
        ...

    def models(self) -> list[str]:
        """All known model ids, no particular order."""
        ...


@runtime_checkable
class CacheLookupProtocol(Protocol):
    """Salvage oracle for cancelled speculations (a backward seek may re-hit, §4.8)."""

    def is_salvageable(self, shot_key: str) -> bool:
        """Whether a cancelled shot's asset is worth keeping (re-read likely)."""
        ...

    def has(self, shot_key: str) -> bool:
        """Whether a finished asset for ``shot_key`` is already cached (free hit)."""
        ...


@runtime_checkable
class SpeculativeBudgetProtocol(Protocol):
    """The speculative-spend ledger seam (a *cap*, distinct from the §11 video budget).

    The engine reserves before launching and releases on cancellation. ``remaining``
    is the hard cap the portfolio optimiser must never exceed; ``spent`` /
    ``reserved`` let the accounting loop reason about realised vs. at-risk dollars.
    """

    def remaining_usd(self) -> float:
        """Speculative dollars still available to reserve (never negative)."""
        ...

    @property
    def reserved_usd(self) -> float:
        """Dollars currently at-risk (reserved, not yet settled or released)."""
        ...

    @property
    def spent_usd(self) -> float:
        """Dollars realised as spend (settled reservations); never refundable."""
        ...

    def reserve(self, usd: float) -> bool:
        """Reserve ``usd`` if it fits under the cap; return ``True`` on success."""
        ...

    def release(self, usd: float) -> None:
        """Return previously-reserved ``usd`` to the pool (cancellation refund)."""
        ...

    def settle(self, usd: float) -> None:
        """Convert ``usd`` of reservation into realised spend (shot landed/started)."""
        ...


__all__ = [
    "CacheLookupProtocol",
    "Clock",
    "CostModelProtocol",
    "SpeculativeBudgetProtocol",
]
