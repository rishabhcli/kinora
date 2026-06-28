"""FastAPI glue for idempotency keys (kinora.md §12).

:class:`IdempotencyStore` (in :mod:`app.api.realtime.idempotency`) is the storage
state machine; this module is the *route-facing* contract that turns it into a
one-liner a handler can opt into. A handler:

1. resolves :class:`IdempotencyGuard` from the ``Idempotency-Key`` header (a
   no-op passthrough when the header is absent — the key is optional),
2. calls :meth:`IdempotencyGuard.begin`, which either lets it proceed, replays a
   stored response (raising :class:`ReplayResponse` the handler converts to its
   return), reports a 409 in-flight conflict, or a 422 key-reuse mismatch,
3. on success calls :meth:`IdempotencyGuard.record` with the response it built so
   a later retry replays it; on failure calls :meth:`IdempotencyGuard.abort` so a
   crashed handler doesn't wedge the key.

The guard reads the raw request body once (FastAPI has already buffered it for
the pydantic model) to fingerprint the request, scopes keys per user, and stamps
``Idempotency-Key`` + ``Idempotent-Replayed`` response headers so the client can
see whether it got a fresh or replayed result. Absent a key it is transparent —
non-idempotent callers behave exactly as before.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from fastapi import Request, Response

from app.api.errors import APIError
from app.api.realtime.idempotency import IdempotencyStore, fingerprint
from app.api.realtime.services import get_realtime
from app.core.logging import get_logger

logger = get_logger("app.api.realtime.idempotency_dep")

#: The request header carrying a client-minted idempotency key.
HEADER = "Idempotency-Key"
#: Max key length we accept (a UUID/ULID is comfortably under this).
_MAX_KEY_LEN = 200


class ReplayResponse(Exception):  # noqa: N818 - a control-flow signal, not an error
    """Raised by :meth:`IdempotencyGuard.begin` to short-circuit with a replay.

    The realtime router's handlers catch this and return its ``status``/``body``;
    a generic catcher is also installed so any route using the guard works without
    a try/except (see :func:`install_replay_handler`).
    """

    def __init__(self, status: int, body: dict[str, Any]) -> None:
        super().__init__("idempotent replay")
        self.status = status
        self.body = body


@dataclass(slots=True)
class IdempotencyGuard:
    """Per-request idempotency helper bound to the user + the request fingerprint."""

    store: IdempotencyStore
    response: Response
    user_id: str
    scope: str
    key: str | None
    fingerprint: str
    _began: bool = False

    @property
    def active(self) -> bool:
        """Whether a key was supplied (idempotency is engaged for this request)."""
        return self.key is not None

    async def begin(self) -> None:
        """Claim the key. No-op without a key; may raise replay/409/422 otherwise."""
        if self.key is None:
            return
        self.response.headers[HEADER] = self.key
        outcome = await self.store.begin(
            user_id=self.user_id,
            scope=self.scope,
            idem_key=self.key,
            request_fingerprint=self.fingerprint,
        )
        if outcome.replay is not None:
            self.response.headers["Idempotent-Replayed"] = "true"
            raise ReplayResponse(outcome.replay.status, outcome.replay.body)
        if outcome.conflict:
            raise APIError(
                "idempotency_conflict",
                "a request with this Idempotency-Key is still in flight",
                status=409,
                detail={"scope": self.scope},
            )
        if outcome.mismatch:
            raise APIError(
                "idempotency_key_reuse",
                "this Idempotency-Key was used for a different request",
                status=422,
                detail={"scope": self.scope},
            )
        self.response.headers["Idempotent-Replayed"] = "false"
        self._began = True

    async def record(self, status: int, body: dict[str, Any]) -> None:
        """Store the response so a retry replays it (no-op without a key)."""
        if self.key is None or not self._began:
            return
        await self.store.record(
            user_id=self.user_id,
            scope=self.scope,
            idem_key=self.key,
            request_fingerprint=self.fingerprint,
            status=status,
            body=body,
        )

    async def abort(self) -> None:
        """Release a pending claim after a handler failure (no-op without a key)."""
        if self.key is None or not self._began:
            return
        await self.store.release(user_id=self.user_id, scope=self.scope, idem_key=self.key)


async def build_guard(
    request: Request, response: Response, *, user_id: str, scope: str
) -> IdempotencyGuard:
    """Construct a guard for ``request`` (reads the body to fingerprint it)."""
    key = request.headers.get(HEADER)
    if key is not None:
        key = key.strip()[:_MAX_KEY_LEN] or None
    body = await request.body()
    realtime = get_realtime(request)
    return IdempotencyGuard(
        store=realtime.idempotency,
        response=response,
        user_id=user_id,
        scope=scope,
        key=key,
        fingerprint=fingerprint(request.method, request.url.path, body),
    )


__all__ = [
    "HEADER",
    "IdempotencyGuard",
    "ReplayResponse",
    "build_guard",
]
