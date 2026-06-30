"""Idempotency-Key support for unsafe POSTs (store-and-replay within a window).

A client retrying a ``POST`` (a flaky network, an impatient double-click) must
not create two sessions / charge twice. The IETF *Idempotency-Key* pattern fixes
this: the client sends a unique ``Idempotency-Key`` header; the server runs the
operation **once**, stores the response, and **replays the stored response
verbatim** for every later request carrying the same key — within a TTL window.

:class:`IdempotencyMiddleware` implements this for the configured methods/paths:

* First request for a key: it runs the route, captures the full response (status,
  headers, body) *only on success* (a 2xx; failures are not cached so the client
  can retry a transient error), stores it, and adds ``Idempotency-Replayed: false``.
* A concurrent second request *while the first is in flight* gets a ``409
  idempotency_in_progress`` (no double-execution).
* A later request with the same key replays the stored response and adds
  ``Idempotency-Replayed: true``.
* A request reusing a key with a **different body/path** (a key-reuse mistake)
  gets a ``422 idempotency_key_reuse`` — the key is bound to its first request's
  fingerprint, so a stored response is never returned for a different operation.

Two stores share one :class:`IdempotencyStore` protocol — an in-process
:class:`InMemoryIdempotencyStore` (default / tests) and a
:class:`RedisIdempotencyStore` (shared across replicas, with a lock so only one
replica executes a given key). Fail-open: a store outage degrades to "no
idempotency" (the route just runs), never a ``5xx``.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any, Protocol

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.apihardening.config import HardeningConfig
from app.apihardening.render import render_error_bytes
from app.core.logging import get_logger

logger = get_logger("app.apihardening.idempotency")

#: A safe subset of response headers to persist + replay (avoid hop-by-hop / id
#: headers that must reflect the *current* request, e.g. the request-id).
_REPLAYABLE_HEADER_DENYLIST = frozenset(
    {
        b"date",
        b"server",
        b"x-request-id",
        b"x-correlation-id",
        b"ratelimit-limit",
        b"ratelimit-remaining",
        b"ratelimit-reset",
    }
)


@dataclass(slots=True)
class IdempotencyRecord:
    """A persisted response keyed by an idempotency key + request fingerprint."""

    fingerprint: str
    status: int
    headers: list[tuple[str, str]]
    body: bytes

    def to_json(self) -> str:
        import base64

        return json.dumps(
            {
                "fingerprint": self.fingerprint,
                "status": self.status,
                "headers": self.headers,
                "body": base64.b64encode(self.body).decode("ascii"),
            },
            separators=(",", ":"),
        )

    @classmethod
    def from_json(cls, raw: str) -> IdempotencyRecord:
        import base64

        data = json.loads(raw)
        return cls(
            fingerprint=data["fingerprint"],
            status=int(data["status"]),
            headers=[(k, v) for k, v in data["headers"]],
            body=base64.b64decode(data["body"]),
        )


class IdempotencyStore(Protocol):
    """Backend protocol for storing/replaying idempotent responses."""

    async def begin(self, key: str, fingerprint: str, *, ttl_s: int) -> str:
        """Reserve ``key`` for execution. Returns one of:

        * ``"new"`` — reserved by this caller; proceed to run the route.
        * ``"in_progress"`` — another caller holds the reservation; reject 409.
        * ``"replay"`` — a completed record exists; the caller should replay it.
        """
        ...

    async def get(self, key: str) -> IdempotencyRecord | None:
        """Return the stored record for ``key`` (``None`` if absent/in-progress)."""
        ...

    async def complete(self, key: str, record: IdempotencyRecord, *, ttl_s: int) -> None:
        """Persist the completed ``record`` (and clear the in-progress marker)."""
        ...

    async def abort(self, key: str) -> None:
        """Release an in-progress reservation without storing a record (on failure)."""
        ...


class InMemoryIdempotencyStore:
    """A process-local idempotency store with TTL expiry (single instance/tests)."""

    def __init__(self, *, clock: Any = time.monotonic) -> None:
        # key -> ("pending"|"done", expiry, record|None, fingerprint)
        self._entries: dict[str, tuple[str, float, IdempotencyRecord | None, str]] = {}
        self._clock = clock

    def _expired(self, key: str) -> bool:
        entry = self._entries.get(key)
        return entry is not None and entry[1] <= self._clock()

    async def begin(self, key: str, fingerprint: str, *, ttl_s: int) -> str:
        if self._expired(key):
            self._entries.pop(key, None)
        entry = self._entries.get(key)
        if entry is None:
            self._entries[key] = ("pending", self._clock() + ttl_s, None, fingerprint)
            return "new"
        state = entry[0]
        if state == "done":
            return "replay"
        return "in_progress"

    async def get(self, key: str) -> IdempotencyRecord | None:
        if self._expired(key):
            self._entries.pop(key, None)
            return None
        entry = self._entries.get(key)
        if entry is None or entry[0] != "done":
            return None
        return entry[2]

    async def complete(self, key: str, record: IdempotencyRecord, *, ttl_s: int) -> None:
        self._entries[key] = ("done", self._clock() + ttl_s, record, record.fingerprint)

    async def abort(self, key: str) -> None:
        entry = self._entries.get(key)
        if entry is not None and entry[0] == "pending":
            self._entries.pop(key, None)

    def clear(self) -> None:
        """Drop all entries (test helper)."""
        self._entries.clear()


class RedisIdempotencyStore:
    """A Redis-backed idempotency store, shared across replicas.

    Uses ``SET NX PX`` to reserve a key (so exactly one replica runs it) and a
    separate ``:rec`` key for the completed record. ``redis`` may be a client or a
    zero-arg resolver returning one (resolved lazily — see
    :func:`app.apihardening.ratelimit.resolve_redis`). Fail-open via the caller:
    the middleware catches store errors and runs the route un-deduplicated; a
    not-yet-available client raises and is treated the same way.
    """

    def __init__(self, redis: Any, *, key_prefix: str = "kinora:harden:idem") -> None:
        self._redis = redis
        self._prefix = key_prefix

    def _raw(self) -> Any:
        from app.apihardening.ratelimit import resolve_redis

        client = resolve_redis(self._redis)
        if client is None:
            raise RuntimeError("redis is not available")
        return client.raw

    def _lock_key(self, key: str) -> str:
        return f"{self._prefix}:lock:{key}"

    def _rec_key(self, key: str) -> str:
        return f"{self._prefix}:rec:{key}"

    async def begin(self, key: str, fingerprint: str, *, ttl_s: int) -> str:
        raw = self._raw()
        existing = await raw.get(self._rec_key(key))
        if existing is not None:
            return "replay"
        acquired = await raw.set(
            self._lock_key(key), fingerprint, nx=True, px=ttl_s * 1000
        )
        if acquired:
            return "new"
        # Lost the race: a record may now exist, else another replica is running.
        existing = await raw.get(self._rec_key(key))
        return "replay" if existing is not None else "in_progress"

    async def get(self, key: str) -> IdempotencyRecord | None:
        raw = await self._raw().get(self._rec_key(key))
        if raw is None:
            return None
        return IdempotencyRecord.from_json(raw)

    async def complete(self, key: str, record: IdempotencyRecord, *, ttl_s: int) -> None:
        raw = self._raw()
        await raw.set(self._rec_key(key), record.to_json(), ex=ttl_s)
        await raw.delete(self._lock_key(key))

    async def abort(self, key: str) -> None:
        await self._raw().delete(self._lock_key(key))


def _fingerprint(method: str, path: str, body: bytes) -> str:
    """A stable fingerprint of the request bound to an idempotency key."""
    digest = hashlib.sha256()
    digest.update(method.upper().encode("ascii"))
    digest.update(b"\0")
    digest.update(path.encode("utf-8"))
    digest.update(b"\0")
    digest.update(body)
    return digest.hexdigest()


class IdempotencyMiddleware:
    """Store-and-replay responses for unsafe POSTs carrying an Idempotency-Key."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        store: IdempotencyStore | None = None,
        config: HardeningConfig | None = None,
    ) -> None:
        self.app = app
        self._config = config or HardeningConfig()
        self._store = store or InMemoryIdempotencyStore()
        self._header = self._config.idempotency_header.lower().encode("latin-1")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        method = scope.get("method", "GET").upper()
        path = scope.get("path", "")
        cfg = self._config
        if method not in cfg.idempotent_methods:
            await self.app(scope, receive, send)
            return
        if cfg.idempotency_path_prefixes and not any(
            path.startswith(p) for p in cfg.idempotency_path_prefixes
        ):
            await self.app(scope, receive, send)
            return

        headers = {name.lower(): value for name, value in scope.get("headers", [])}
        key_raw = headers.get(self._header)
        if key_raw is None:
            await self.app(scope, receive, send)
            return
        key = key_raw.decode("latin-1").strip()
        if not key or len(key) > cfg.idempotency_key_max_len:
            await self._reject(
                send,
                code="invalid_idempotency_key",
                title="Invalid Idempotency-Key",
                status=400,
                detail="the Idempotency-Key header is empty or too long",
            )
            return

        # Buffer the body once so we can fingerprint it and still hand it to the app.
        body = await _read_body(receive)
        fingerprint = _fingerprint(method, path, body)

        try:
            outcome = await self._store.begin(key, fingerprint, ttl_s=cfg.idempotency_ttl_s)
        except Exception as exc:  # noqa: BLE001 - fail open
            logger.warning("idempotency.store_unavailable", error=str(exc))
            await self.app(scope, _replay_body(body), send)
            return

        if outcome == "replay":
            record = await self._store.get(key)
            if record is not None:
                if record.fingerprint != fingerprint:
                    await self._reject(
                        send,
                        code="idempotency_key_reuse",
                        title="Idempotency-Key Reuse",
                        status=422,
                        detail="this Idempotency-Key was used for a different request",
                    )
                    return
                await self._replay(send, record)
                return
            # Race: record vanished (TTL) between begin and get — run fresh.
            outcome = "new"

        if outcome == "in_progress":
            await self._reject(
                send,
                code="idempotency_in_progress",
                title="Request In Progress",
                status=409,
                detail="a request with this Idempotency-Key is still being processed",
            )
            return

        # outcome == "new": run the route, capturing the response.
        await self._run_and_capture(scope, body, send, key, fingerprint)

    async def _run_and_capture(
        self, scope: Scope, body: bytes, send: Send, key: str, fingerprint: str
    ) -> None:
        captured_status = 0
        captured_headers: list[tuple[bytes, bytes]] = []
        body_chunks: list[bytes] = []
        cfg = self._config

        async def capturing_send(message: Message) -> None:
            nonlocal captured_status, captured_headers
            if message["type"] == "http.response.start":
                captured_status = int(message["status"])
                captured_headers = list(message.get("headers", []))
                raw = list(captured_headers)
                raw.append((b"idempotency-replayed", b"false"))
                message = {**message, "headers": raw}
            elif message["type"] == "http.response.body":
                body_chunks.append(message.get("body", b""))
            await send(message)

        try:
            await self.app(scope, _replay_body(body), capturing_send)
        except Exception:
            # The route raised (will surface as a 500 via the app's handler chain);
            # release the reservation so a retry is allowed, and re-raise.
            with _SuppressStoreError():
                await self._store.abort(key)
            raise

        # Only persist successful (2xx) responses — a transient failure must stay
        # retryable rather than be replayed forever.
        if 200 <= captured_status < 300:
            record = IdempotencyRecord(
                fingerprint=fingerprint,
                status=captured_status,
                headers=_persistable_headers(captured_headers),
                body=b"".join(body_chunks),
            )
            with _SuppressStoreError():
                await self._store.complete(key, record, ttl_s=cfg.idempotency_ttl_s)
        else:
            with _SuppressStoreError():
                await self._store.abort(key)

    async def _replay(self, send: Send, record: IdempotencyRecord) -> None:
        headers = [(k.encode("latin-1"), v.encode("latin-1")) for k, v in record.headers]
        headers.append((b"idempotency-replayed", b"true"))
        await send(
            {"type": "http.response.start", "status": record.status, "headers": headers}
        )
        await send({"type": "http.response.body", "body": record.body})

    async def _reject(
        self,
        send: Send,
        *,
        code: str,
        title: str,
        status: int,
        detail: str,
    ) -> None:
        payload, media_type = render_error_bytes(
            code=code, title=title, status=status, detail=detail, config=self._config
        )
        headers = [
            (b"content-type", media_type.encode("ascii")),
            (b"content-length", str(len(payload)).encode("ascii")),
        ]
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": payload})


async def _read_body(receive: Receive) -> bytes:
    """Drain the full request body from the ASGI receive channel."""
    chunks: list[bytes] = []
    more = True
    while more:
        message = await receive()
        if message["type"] == "http.request":
            chunks.append(message.get("body", b""))
            more = message.get("more_body", False)
        elif message["type"] == "http.disconnect":
            break
    return b"".join(chunks)


def _replay_body(body: bytes) -> Receive:
    """Build a one-shot ``receive`` that re-emits a previously buffered body."""
    sent = False

    async def receive() -> Message:
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    return receive


def _persistable_headers(headers: list[tuple[bytes, bytes]]) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for name, value in headers:
        if name.lower() in _REPLAYABLE_HEADER_DENYLIST:
            continue
        out.append((name.decode("latin-1"), value.decode("latin-1")))
    return out


class _SuppressStoreError:
    """A tiny ``contextlib.suppress(Exception)`` that also logs (fail-open stores)."""

    def __enter__(self) -> _SuppressStoreError:
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        if exc is not None and isinstance(exc, Exception):
            logger.warning("idempotency.store_write_failed", error=str(exc))
            return True
        return False


__all__ = [
    "IdempotencyMiddleware",
    "IdempotencyRecord",
    "IdempotencyStore",
    "InMemoryIdempotencyStore",
    "RedisIdempotencyStore",
]
