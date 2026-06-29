"""The RPC error taxonomy — gRPC-shaped status codes + retry classification.

Every failure that crosses an RPC seam is normalized into an :class:`RpcError`
carrying an :class:`RpcStatus` code. The code shape mirrors gRPC's canonical
status codes (and maps cleanly onto HTTP) so the *same* error model is correct
whether a call ran in-process, over the loopback transport, or — later — over a
real HTTP/gRPC wire. That uniformity is the whole point: a caller's resilience
policy (retry, hedge, circuit-break) reasons about the **status**, never about
which transport happened to serve the call.

Two orthogonal axes drive the resilience policies:

* **retryability** (:meth:`RpcStatus.retryable`) — whether *re-issuing the same
  request* could plausibly succeed. ``UNAVAILABLE`` / ``DEADLINE_EXCEEDED`` are
  retryable; ``INVALID_ARGUMENT`` / ``NOT_FOUND`` are not.
* **failure kind** (:attr:`RpcError.kind`) — *transport* (the call never reached
  application code: refused, timed out, no endpoint) vs *application* (the method
  ran and raised / returned an error). A circuit breaker counts transport
  failures heavily; an application error from a healthy server should not trip it.

This module is dependency-free and import-cheap.
"""

from __future__ import annotations

import enum
from typing import Any


class RpcStatus(enum.IntEnum):
    """Canonical RPC status codes (gRPC-shaped; HTTP-mappable).

    Integer values match gRPC's canonical codes so the enum is wire-stable: a
    real gRPC transport added later can pass codes through untranslated.
    """

    OK = 0
    CANCELLED = 1
    UNKNOWN = 2
    INVALID_ARGUMENT = 3
    DEADLINE_EXCEEDED = 4
    NOT_FOUND = 5
    ALREADY_EXISTS = 6
    PERMISSION_DENIED = 7
    RESOURCE_EXHAUSTED = 8
    FAILED_PRECONDITION = 9
    ABORTED = 10
    OUT_OF_RANGE = 11
    UNIMPLEMENTED = 12
    INTERNAL = 13
    UNAVAILABLE = 14
    DATA_LOSS = 15
    UNAUTHENTICATED = 16

    @property
    def retryable(self) -> bool:
        """Whether re-issuing the identical request could plausibly succeed.

        Follows gRPC's idempotent-retry guidance: only codes that denote a
        *transient* server-side condition (or a cancelled/aborted attempt) are
        retryable. ``RESOURCE_EXHAUSTED`` is retryable because it is the
        backpressure / rate-limit signal that backoff is designed for.
        """
        return self in _RETRYABLE

    @property
    def is_client_error(self) -> bool:
        """True for codes that blame the request itself (4xx-shaped)."""
        return self in _CLIENT_ERRORS

    def to_http(self) -> int:
        """Map the status onto the closest HTTP status code (for an HTTP wire)."""
        return _HTTP_BY_STATUS.get(self, 500)

    @classmethod
    def from_http(cls, http_status: int) -> RpcStatus:
        """Map an HTTP status code back onto the closest RPC status."""
        if 200 <= http_status < 300:
            return cls.OK
        return _STATUS_BY_HTTP.get(http_status, cls.UNKNOWN if http_status < 500 else cls.INTERNAL)


_RETRYABLE: frozenset[RpcStatus] = frozenset(
    {
        RpcStatus.CANCELLED,
        RpcStatus.DEADLINE_EXCEEDED,
        RpcStatus.RESOURCE_EXHAUSTED,
        RpcStatus.ABORTED,
        RpcStatus.UNAVAILABLE,
    }
)

_CLIENT_ERRORS: frozenset[RpcStatus] = frozenset(
    {
        RpcStatus.INVALID_ARGUMENT,
        RpcStatus.NOT_FOUND,
        RpcStatus.ALREADY_EXISTS,
        RpcStatus.PERMISSION_DENIED,
        RpcStatus.FAILED_PRECONDITION,
        RpcStatus.OUT_OF_RANGE,
        RpcStatus.UNAUTHENTICATED,
        RpcStatus.UNIMPLEMENTED,
    }
)

_HTTP_BY_STATUS: dict[RpcStatus, int] = {
    RpcStatus.OK: 200,
    RpcStatus.CANCELLED: 499,
    RpcStatus.UNKNOWN: 500,
    RpcStatus.INVALID_ARGUMENT: 400,
    RpcStatus.DEADLINE_EXCEEDED: 504,
    RpcStatus.NOT_FOUND: 404,
    RpcStatus.ALREADY_EXISTS: 409,
    RpcStatus.PERMISSION_DENIED: 403,
    RpcStatus.RESOURCE_EXHAUSTED: 429,
    RpcStatus.FAILED_PRECONDITION: 412,
    RpcStatus.ABORTED: 409,
    RpcStatus.OUT_OF_RANGE: 400,
    RpcStatus.UNIMPLEMENTED: 501,
    RpcStatus.INTERNAL: 500,
    RpcStatus.UNAVAILABLE: 503,
    RpcStatus.DATA_LOSS: 500,
    RpcStatus.UNAUTHENTICATED: 401,
}

_STATUS_BY_HTTP: dict[int, RpcStatus] = {
    400: RpcStatus.INVALID_ARGUMENT,
    401: RpcStatus.UNAUTHENTICATED,
    403: RpcStatus.PERMISSION_DENIED,
    404: RpcStatus.NOT_FOUND,
    409: RpcStatus.ALREADY_EXISTS,
    412: RpcStatus.FAILED_PRECONDITION,
    429: RpcStatus.RESOURCE_EXHAUSTED,
    499: RpcStatus.CANCELLED,
    500: RpcStatus.INTERNAL,
    501: RpcStatus.UNIMPLEMENTED,
    503: RpcStatus.UNAVAILABLE,
    504: RpcStatus.DEADLINE_EXCEEDED,
}


class FailureKind(enum.Enum):
    """Whether a failure was transport-level or application-level.

    * :attr:`TRANSPORT` — the request never reached (or never returned from)
      application code: connection refused, timed out at the socket, no healthy
      endpoint, serialization broke. These are the failures a **circuit breaker**
      should weigh, because they signal an unhealthy *endpoint*.
    * :attr:`APPLICATION` — the method ran and produced an error result. A
      well-behaved server returning ``NOT_FOUND`` is healthy; counting that
      against the breaker would trip it on normal traffic.
    """

    TRANSPORT = "transport"
    APPLICATION = "application"


class RpcError(Exception):
    """A normalized RPC failure (status code + kind + structured detail).

    Carries enough to drive every downstream policy without the policy needing to
    know the transport: :attr:`status` (what went wrong), :attr:`kind` (transport
    vs application), :attr:`retryable` (cached from the status), and an optional
    :attr:`detail` payload for structured error bodies.
    """

    def __init__(
        self,
        status: RpcStatus,
        message: str,
        *,
        kind: FailureKind = FailureKind.APPLICATION,
        detail: Any = None,
        service: str | None = None,
        method: str | None = None,
        cause: BaseException | None = None,
    ) -> None:
        self.status = status
        self.kind = kind
        self.detail = detail
        self.service = service
        self.method = method
        super().__init__(message)
        if cause is not None:
            self.__cause__ = cause

    @property
    def retryable(self) -> bool:
        """Whether a resilience policy may re-issue the request."""
        return self.status.retryable

    @property
    def is_transport(self) -> bool:
        """True when the failure never reached application code."""
        return self.kind is FailureKind.TRANSPORT

    def with_endpoint(self, service: str, method: str) -> RpcError:
        """Return a copy annotated with the service/method that failed."""
        return RpcError(
            self.status,
            str(self),
            kind=self.kind,
            detail=self.detail,
            service=service,
            method=method,
            cause=self.__cause__,
        )

    def to_dict(self) -> dict[str, Any]:
        """A JSON-serializable view (for the loopback/HTTP transports)."""
        return {
            "status": int(self.status),
            "code": self.status.name,
            "message": str(self),
            "kind": self.kind.value,
            "detail": self.detail,
            "service": self.service,
            "method": self.method,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RpcError:
        """Reconstruct an :class:`RpcError` from :meth:`to_dict` output."""
        return cls(
            RpcStatus(int(data["status"])),
            str(data.get("message", "")),
            kind=FailureKind(data.get("kind", "application")),
            detail=data.get("detail"),
            service=data.get("service"),
            method=data.get("method"),
        )

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        loc = f"{self.service}.{self.method}" if self.service else "?"
        return f"RpcError({self.status.name} {self.kind.value} {loc!s}: {self!s})"


# --------------------------------------------------------------------------- #
# Convenience constructors for the common cases.
# --------------------------------------------------------------------------- #


def deadline_exceeded(message: str = "deadline exceeded", **kw: Any) -> RpcError:
    """A transport-kind ``DEADLINE_EXCEEDED`` (the call ran out of time budget)."""
    kw.setdefault("kind", FailureKind.TRANSPORT)
    return RpcError(RpcStatus.DEADLINE_EXCEEDED, message, **kw)


def unavailable(message: str = "service unavailable", **kw: Any) -> RpcError:
    """A transport-kind ``UNAVAILABLE`` (no endpoint / refused / breaker open)."""
    kw.setdefault("kind", FailureKind.TRANSPORT)
    return RpcError(RpcStatus.UNAVAILABLE, message, **kw)


def cancelled(message: str = "request cancelled", **kw: Any) -> RpcError:
    """A transport-kind ``CANCELLED`` (caller aborted, e.g. seek cancellation)."""
    kw.setdefault("kind", FailureKind.TRANSPORT)
    return RpcError(RpcStatus.CANCELLED, message, **kw)


def not_found(message: str = "not found", **kw: Any) -> RpcError:
    """An application-kind ``NOT_FOUND`` (the method ran; the entity is absent)."""
    return RpcError(RpcStatus.NOT_FOUND, message, **kw)


def invalid_argument(message: str = "invalid argument", **kw: Any) -> RpcError:
    """An application-kind ``INVALID_ARGUMENT`` (the request payload is wrong)."""
    return RpcError(RpcStatus.INVALID_ARGUMENT, message, **kw)


def unimplemented(message: str = "method not implemented", **kw: Any) -> RpcError:
    """An application-kind ``UNIMPLEMENTED`` (the contract method has no impl)."""
    return RpcError(RpcStatus.UNIMPLEMENTED, message, **kw)


def internal(message: str = "internal error", **kw: Any) -> RpcError:
    """An application-kind ``INTERNAL`` (the method raised an unexpected error)."""
    return RpcError(RpcStatus.INTERNAL, message, **kw)


def resource_exhausted(message: str = "resource exhausted", **kw: Any) -> RpcError:
    """A ``RESOURCE_EXHAUSTED`` (rate-limited / backpressured; retryable)."""
    return RpcError(RpcStatus.RESOURCE_EXHAUSTED, message, **kw)


__all__ = [
    "FailureKind",
    "RpcError",
    "RpcStatus",
    "cancelled",
    "deadline_exceeded",
    "internal",
    "invalid_argument",
    "not_found",
    "resource_exhausted",
    "unavailable",
    "unimplemented",
]
