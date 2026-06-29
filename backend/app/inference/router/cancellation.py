"""Cancellation tokens — cooperative abort tied to a session/trajectory (§12.1).

The Scheduler cancels a job whose target the reader has moved away from (§4.8);
workers check the token at safe points and abort cooperatively, releasing any
reserved capacity. The same mechanism lets a tenant cancel an entire session's
in-flight work in one call.

A :class:`CancellationToken` is a cheap, thread-safe-free (single-loop) flag with
an optional reason and a set of observers fired on trip. A
:class:`CancellationRegistry` groups tokens by an arbitrary scope key (session id,
trajectory id) so the router can cancel a whole scope at once — the §12.1
"cancellation token tied to the session and trajectory."

Backends opt in by accepting a token on the request metadata and polling
:meth:`CancellationToken.raise_if_cancelled` at safe points (before a provider
call, between render and QA). The router itself drops *queued* cancelled requests
at the next tick and never dispatches them.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field


class CancelledError(Exception):  # noqa: N818 - public name in router contract
    """Raised by a backend when it observes a tripped cancellation token."""

    def __init__(self, request_id: str | None = None, reason: str | None = None) -> None:
        super().__init__(reason or "request cancelled")
        self.request_id = request_id
        self.reason = reason


@dataclass(slots=True)
class CancellationToken:
    """A one-way cooperative-cancellation flag.

    Attributes:
        scope: The scope this token belongs to (session/trajectory id).
        cancelled: Whether the token has been tripped.
        reason: Optional human-readable reason recorded on trip.
    """

    scope: str = "default"
    cancelled: bool = False
    reason: str | None = None
    _observers: list[Callable[[], None]] = field(default_factory=list, repr=False)

    def cancel(self, reason: str | None = None) -> bool:
        """Trip the token. Idempotent; returns ``True`` only on the first trip."""
        if self.cancelled:
            return False
        self.cancelled = True
        self.reason = reason
        for obs in self._observers:
            obs()
        return True

    def on_cancel(self, observer: Callable[[], None]) -> None:
        """Register a callback fired when the token trips (immediately if already)."""
        if self.cancelled:
            observer()
        else:
            self._observers.append(observer)

    def raise_if_cancelled(self, request_id: str | None = None) -> None:
        """Backends call this at safe points; raises :class:`CancelledError` if tripped."""
        if self.cancelled:
            raise CancelledError(request_id=request_id, reason=self.reason)


class CancellationRegistry:
    """Groups cancellation tokens by scope so a whole scope cancels at once."""

    def __init__(self) -> None:
        self._by_scope: dict[str, list[CancellationToken]] = {}

    def token(self, scope: str = "default") -> CancellationToken:
        """Mint a fresh token registered under ``scope``."""
        tok = CancellationToken(scope=scope)
        self._by_scope.setdefault(scope, []).append(tok)
        return tok

    def cancel_scope(self, scope: str, reason: str | None = None) -> int:
        """Trip every live token in ``scope``; returns how many newly tripped."""
        tripped = 0
        for tok in self._by_scope.get(scope, ()):
            if tok.cancel(reason):
                tripped += 1
        return tripped

    def active_scopes(self) -> list[str]:
        """Scopes with at least one un-tripped token."""
        return [s for s, toks in self._by_scope.items() if any(not t.cancelled for t in toks)]

    def prune(self) -> int:
        """Drop fully-cancelled scopes; returns the number of scopes removed."""
        dead = [s for s, toks in self._by_scope.items() if all(t.cancelled for t in toks)]
        for s in dead:
            del self._by_scope[s]
        return len(dead)


__all__ = ["CancellationRegistry", "CancellationToken", "CancelledError"]
