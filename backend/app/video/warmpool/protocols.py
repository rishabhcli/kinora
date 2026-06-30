"""The injectable seams the warm pool is pure logic *over*.

Everything the pool touches that could do I/O — opening a provider session
(an auth handshake + HTTP connection + signed-session token), probing it, and
closing it — is hidden behind a small async protocol so the pool itself is pure,
deterministic logic. Production wires a real factory that calls DashScope /
MiniMax; tests wire a fake factory backed by a :class:`~app.video.warmpool.clock.VirtualClock`.

These are **local** protocols (FINAL round: no cross-round imports). They are
intentionally narrower than ``app.providers.video_router.VideoBackend`` — the pool
manages *connections*, not *renders*. A real adapter holds a ``VideoBackend`` and
exposes it through :meth:`ProviderSession.handle`; the render path borrows a warm
session and calls ``handle.render(spec)`` itself. The pool never renders and never
touches the ``KINORA_LIVE_VIDEO`` spend gate.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

#: A provider's stable identity (e.g. ``"dashscope"``, ``"minimax"``). Used as the
#: pool key and in all telemetry.
ProviderId = str


@runtime_checkable
class ProviderSession(Protocol):
    """One warm, reusable connection/auth context to a video provider.

    A session bundles whatever is expensive to create: an authenticated HTTP
    connection, a refreshed bearer/signed-session token, a warmed connection pool.
    The render path borrows it, calls through :attr:`handle`, and returns it.
    """

    #: Stable id of the provider this session belongs to.
    provider: ProviderId

    #: An opaque per-session id (for telemetry / leak tracking). Unique within a pool.
    session_id: str

    @property
    def handle(self) -> Any:
        """The thing callers actually use (e.g. a ``VideoBackend``)."""
        ...

    async def healthy(self) -> bool:
        """Cheap liveness probe — does *not* render or spend. ``True`` if usable."""
        ...

    async def close(self) -> None:
        """Release the underlying connection/token. Idempotent."""
        ...


@runtime_checkable
class SessionFactory(Protocol):
    """Opens fresh provider sessions on demand (the only I/O seam in the pool).

    ``open`` is where cold-start latency actually lives: the auth handshake,
    connection establishment, and signed-session minting. The pool measures the
    wall time of every ``open`` to learn each provider's cold-start cost.
    """

    async def open(self, provider: ProviderId) -> ProviderSession:
        """Open and return a ready-to-use session for ``provider``.

        May raise to signal a provider-side failure (e.g. auth rejected); the pool
        treats a raised ``open`` as an unhealthy signal for that provider.
        """
        ...


@runtime_checkable
class HealthSignal(Protocol):
    """A read-only view of a provider's circuit health (the drain seam).

    The render path already owns a circuit breaker per backend
    (``app.providers.video_router.BackendHealth``). Rather than duplicate it, the
    warm pool reads *availability* through this seam: when a provider's circuit is
    open the pool drains its warm sessions instead of holding dead connections.
    Any object with ``available()`` (the breaker's own method) satisfies this.
    """

    def available(self) -> bool:
        """``True`` when the provider's circuit would let a call through now."""
        ...


__all__ = ["HealthSignal", "ProviderId", "ProviderSession", "SessionFactory"]
