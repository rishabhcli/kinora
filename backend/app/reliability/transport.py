"""The request transport seam — real httpx + a scriptable fake (kinora.md §5.6).

The load runner must issue real HTTP against an explicitly-provided target, but
the runner *logic* (arrival pacing, percentile collection, SLA gating) must be
unit-testable without a live server. So the runner depends only on the
:class:`Transport` protocol; production wires :class:`HttpxTransport`, and tests
wire :class:`FakeTransport` (deterministic latency + scripted faults + a call
recording). This keeps the brief's hard rule — *no real load in tests* — true by
construction: a test cannot accidentally reach the network.

A transport call returns a :class:`Response` (status + elapsed + body); a
connection-level failure (timeout / refused) is surfaced as ``status == 0`` with
a populated ``error`` so the report counts it as a transport failure, not an HTTP
error.
"""

from __future__ import annotations

import random
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class Response:
    """A normalized transport response.

    ``status == 0`` means a transport-level failure (no HTTP response); ``error``
    carries the reason. ``elapsed_ms`` is the wall-clock the call took (the
    runner records this into the latency digest).
    """

    status: int
    elapsed_ms: float
    body: Any = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        """True for a 2xx HTTP response (the default success verdict)."""
        return 200 <= self.status < 300

    @property
    def transport_failure(self) -> bool:
        """True for a connection-level failure (no HTTP status)."""
        return self.status == 0


@runtime_checkable
class Transport(Protocol):
    """The async request surface the load runner depends on."""

    async def request(
        self,
        method: str,
        path: str,
        *,
        json: Any | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> Response:
        """Issue one request and return a normalized :class:`Response`."""
        ...

    async def aclose(self) -> None:
        """Release transport resources (idempotent)."""
        ...


# --------------------------------------------------------------------------- #
# Fake transport — scriptable, deterministic, records every call
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class RecordedCall:
    """One request the :class:`FakeTransport` observed."""

    method: str
    path: str
    json: Any | None
    headers: dict[str, str]


#: A scripted responder: ``(method, path, json) -> Response``. Returning ``None``
#: falls through to the FakeTransport's default behaviour.
Responder = Callable[[str, str, Any | None], Response | None]


@dataclass
class FakeTransport:
    """An in-memory transport that never touches the network (test/dry-run only).

    Behaviour is fully deterministic given ``seed``:

    * latency is drawn from a clamped normal (``base_latency_ms`` ± jitter);
    * each call fails with probability ``fault_rate`` (status ``fault_status``,
      or a transport failure when ``fault_status == 0``);
    * a per-path :data:`Responder` (in ``responders``) can override the status /
      body for specific endpoints (e.g. make ``/auth/login`` return a token).

    Every call is appended to :attr:`calls` so tests can assert *what* the runner
    issued, not just the aggregate report.
    """

    base_latency_ms: float = 8.0
    latency_jitter_ms: float = 2.0
    fault_rate: float = 0.0
    fault_status: int = 503  # 0 => transport failure (status 0)
    default_status: int = 200
    default_body: Any = None
    seed: int = 0
    responders: dict[str, Responder] = field(default_factory=dict)
    calls: list[RecordedCall] = field(default_factory=list)
    _rng: random.Random = field(init=False, repr=False)
    _closed: bool = field(default=False, init=False, repr=False)

    def __post_init__(self) -> None:
        self._rng = random.Random(self.seed)

    def _latency_ms(self) -> float:
        if self.latency_jitter_ms <= 0.0:
            return max(0.0, self.base_latency_ms)
        return max(0.0, self._rng.gauss(self.base_latency_ms, self.latency_jitter_ms))

    async def request(
        self,
        method: str,
        path: str,
        *,
        json: Any | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> Response:
        """Record the call and return a deterministic scripted response."""
        self.calls.append(
            RecordedCall(
                method=method.upper(),
                path=path,
                json=json,
                headers=dict(headers or {}),
            )
        )
        elapsed = self._latency_ms()

        # 1. A per-path responder wins if it returns a Response.
        responder = self.responders.get(path)
        if responder is not None:
            scripted = responder(method.upper(), path, json)
            if scripted is not None:
                return scripted

        # 2. Inject a fault for this call.
        if self.fault_rate > 0.0 and self._rng.random() < self.fault_rate:
            if self.fault_status == 0:
                return Response(
                    status=0, elapsed_ms=elapsed, error="injected transport failure"
                )
            return Response(
                status=self.fault_status,
                elapsed_ms=elapsed,
                error=f"injected fault {self.fault_status}",
            )

        # 3. Default healthy response.
        return Response(status=self.default_status, elapsed_ms=elapsed, body=self.default_body)

    async def aclose(self) -> None:
        """Mark closed (no resources to release)."""
        self._closed = True

    @property
    def closed(self) -> bool:
        """Whether :meth:`aclose` has been called."""
        return self._closed

    def calls_to(self, path: str) -> list[RecordedCall]:
        """Recorded calls whose path equals ``path`` (assertion helper)."""
        return [c for c in self.calls if c.path == path]


# --------------------------------------------------------------------------- #
# Real httpx transport — the production path the CLI wires
# --------------------------------------------------------------------------- #


class HttpxTransport:
    """A thin httpx.AsyncClient wrapper implementing :class:`Transport`.

    Constructed lazily by the CLI against the explicitly-provided ``--target``;
    never imported or instantiated by the unit tests (which use
    :class:`FakeTransport`), so the test process never opens a socket. A timeout
    or connection error is mapped to a ``status == 0`` transport failure rather
    than raised, so one flaky request does not abort a whole load run.
    """

    def __init__(
        self,
        base_url: str,
        *,
        timeout_s: float = 10.0,
        token: str | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        import time

        import httpx  # imported here so the test path never needs the dependency wired

        self._clock = clock or time.perf_counter
        default_headers = {"Authorization": f"Bearer {token}"} if token else None
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=timeout_s,
            headers=default_headers,
        )

    async def request(
        self,
        method: str,
        path: str,
        *,
        json: Any | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> Response:
        """Issue the request; map transport errors to a status-0 failure."""
        import httpx

        start = self._clock()
        try:
            resp = await self._client.request(
                method, path, json=json, headers=dict(headers) if headers else None
            )
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            elapsed_ms = (self._clock() - start) * 1000.0
            return Response(status=0, elapsed_ms=elapsed_ms, error=type(exc).__name__)
        elapsed_ms = (self._clock() - start) * 1000.0
        body: Any
        try:
            body = resp.json()
        except (ValueError, UnicodeDecodeError):
            body = None
        return Response(status=resp.status_code, elapsed_ms=elapsed_ms, body=body)

    async def aclose(self) -> None:
        """Close the underlying httpx client."""
        await self._client.aclose()


def token_responder(token: str = "fake-token", *, status: int = 200) -> Responder:
    """A :class:`Responder` that returns an auth-token body (for FakeTransport).

    Lets a test/dry-run script ``/auth/login`` so the runner's login step gets a
    plausible ``{"access_token": ...}`` body without a server.
    """

    def _respond(method: str, path: str, json: Any | None) -> Response | None:
        return Response(
            status=status,
            elapsed_ms=0.0,
            body={"access_token": token, "token_type": "bearer", "expires_in": 86400},
        )

    return _respond


__all__ = [
    "FakeTransport",
    "HttpxTransport",
    "RecordedCall",
    "Responder",
    "Response",
    "Transport",
    "token_responder",
]
