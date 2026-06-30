"""The single result type every limiter and the hierarchy speak in.

A limiter's job is to answer one question — *may this request of ``cost`` units
proceed right now?* — and, if not, *how long until it could*. :class:`Decision`
is that answer, uniform across token-bucket, sliding-window, and GCRA so the
hierarchy can compare and combine them with one rule (most-restrictive wins).

``allowed`` is the verdict. ``retry_after`` is the precise wait when denied (and
``0.0`` when allowed). ``remaining`` is best-effort headroom for ``X-RateLimit-*``
headers — not all algorithms define it identically, so it is advisory. ``reset_after``
is when the limit fully replenishes, also for headers. ``scope``/``limit`` label
which rule produced the decision, for diagnostics and metrics.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Decision:
    """A limiter verdict plus the numbers a caller / HTTP layer needs."""

    allowed: bool
    #: Seconds until the request would be admitted. ``0.0`` when ``allowed``.
    retry_after: float = 0.0
    #: Best-effort remaining capacity (for ``X-RateLimit-Remaining``).
    remaining: float = 0.0
    #: Seconds until the limit fully resets (for ``X-RateLimit-Reset``).
    reset_after: float = 0.0
    #: Dotted scope key of the limit that produced this (diagnostics).
    scope: str = ""
    #: Algorithm label, e.g. ``"token_bucket"`` (diagnostics).
    limit: str = ""

    @classmethod
    def allow(
        cls,
        *,
        remaining: float = 0.0,
        reset_after: float = 0.0,
        scope: str = "",
        limit: str = "",
    ) -> Decision:
        return cls(
            allowed=True,
            retry_after=0.0,
            remaining=remaining,
            reset_after=reset_after,
            scope=scope,
            limit=limit,
        )

    @classmethod
    def deny(
        cls,
        retry_after: float,
        *,
        remaining: float = 0.0,
        reset_after: float = 0.0,
        scope: str = "",
        limit: str = "",
    ) -> Decision:
        return cls(
            allowed=False,
            retry_after=max(0.0, retry_after),
            remaining=remaining,
            reset_after=reset_after,
            scope=scope,
            limit=limit,
        )


__all__ = ["Decision"]
