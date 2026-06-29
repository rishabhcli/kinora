"""The RPC request / response envelopes that cross a transport.

A transport speaks in :class:`RpcRequest` / :class:`RpcResponse` — never in a
Python call. The request names the *logical* endpoint (``service`` + ``method``),
carries the already-validated ``payload`` (a plain ``dict`` after contract
encoding), the propagation ``headers`` (from :meth:`RequestContext.to_headers`),
and the ``attempt`` index (so a retry/hedge is visible to the server for logging
and idempotency). The response is *result-or-error*: either ``ok`` with a body or
``error`` with a normalized :class:`RpcError`-shaped status — never both. Keeping
errors *in band* (a response, not a raised exception) is what lets the loopback
and real-network transports carry an application error across the wire without a
stack trace, exactly like gRPC trailers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.distributed.rpc.errors import FailureKind, RpcError, RpcStatus


@dataclass(frozen=True, slots=True)
class RpcRequest:
    """One logical RPC call as it crosses a transport.

    ``payload`` is the contract-encoded request body (a JSON-ready dict).
    ``headers`` carry the propagated context (trace/auth/tenant/deadline/baggage).
    ``attempt`` starts at 0 and increments per retry/hedge so the server and the
    metrics can see re-issues.
    """

    service: str
    method: str
    payload: dict[str, Any] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)
    attempt: int = 0

    @property
    def endpoint(self) -> str:
        """The ``service.method`` endpoint string (for logs / metrics labels)."""
        return f"{self.service}.{self.method}"

    def for_attempt(self, attempt: int) -> RpcRequest:
        """Return a copy stamped with a new attempt index (retry/hedge)."""
        return RpcRequest(
            service=self.service,
            method=self.method,
            payload=self.payload,
            headers=dict(self.headers),
            attempt=attempt,
        )

    def with_headers(self, headers: dict[str, str]) -> RpcRequest:
        """Return a copy with merged/overridden headers."""
        merged = dict(self.headers)
        merged.update(headers)
        return RpcRequest(
            service=self.service,
            method=self.method,
            payload=self.payload,
            headers=merged,
            attempt=self.attempt,
        )


@dataclass(frozen=True, slots=True)
class RpcResponse:
    """A result-or-error response. Exactly one of ``body`` / ``error`` is set.

    Build a success with :meth:`success` and a failure with :meth:`failure`.
    ``trailers`` carry server→client metadata (e.g. ``served_by`` endpoint,
    cache hit/miss) the client surfaces for observability.
    """

    status: RpcStatus = RpcStatus.OK
    body: Any = None
    error_message: str | None = None
    error_kind: FailureKind = FailureKind.APPLICATION
    error_detail: Any = None
    trailers: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        """True when the call succeeded (``status == OK``)."""
        return self.status is RpcStatus.OK

    @classmethod
    def success(cls, body: Any = None, *, trailers: dict[str, str] | None = None) -> RpcResponse:
        """A successful response carrying ``body``."""
        return cls(status=RpcStatus.OK, body=body, trailers=dict(trailers or {}))

    @classmethod
    def failure(
        cls,
        status: RpcStatus,
        message: str,
        *,
        kind: FailureKind = FailureKind.APPLICATION,
        detail: Any = None,
        trailers: dict[str, str] | None = None,
    ) -> RpcResponse:
        """An error response (never ``OK``)."""
        if status is RpcStatus.OK:
            raise ValueError("an error response cannot carry status OK")
        return cls(
            status=status,
            error_message=message,
            error_kind=kind,
            error_detail=detail,
            trailers=dict(trailers or {}),
        )

    @classmethod
    def from_error(cls, err: RpcError) -> RpcResponse:
        """Wrap an :class:`RpcError` as an error response."""
        return cls.failure(err.status, str(err), kind=err.kind, detail=err.detail)

    def to_error(self, *, service: str | None = None, method: str | None = None) -> RpcError:
        """Reconstruct the :class:`RpcError` for an error response.

        Raises ``ValueError`` if called on a success response — callers should
        guard on :attr:`ok` first.
        """
        if self.ok:
            raise ValueError("cannot convert a successful response to an error")
        return RpcError(
            self.status,
            self.error_message or self.status.name,
            kind=self.error_kind,
            detail=self.error_detail,
            service=service,
            method=method,
        )

    def raise_for_status(self, *, service: str | None = None, method: str | None = None) -> Any:
        """Return ``body`` on success, else raise the reconstructed error."""
        if self.ok:
            return self.body
        raise self.to_error(service=service, method=method)


__all__ = ["RpcRequest", "RpcResponse"]
