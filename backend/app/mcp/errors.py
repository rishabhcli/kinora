"""A typed MCP error taxonomy for the canon-memory server (kinora.md §8.3 / §12).

The official ``mcp`` SDK surfaces a tool failure either as a JSON-RPC protocol
error (when a request is malformed) or as a tool result with ``isError=True``
(when a *call* fails for a domain reason). Kinora's tool surface needs to tell
those apart precisely — a forged ``book_id`` is an authorization denial, an
out-of-range ``episodic_k`` is a request-validation failure, and an unknown tool
name is a method error. This module gives every failure a stable **code** and a
machine-readable **category** so clients (and the conformance suite) can branch
on the reason rather than parsing a free-text string.

The codes are aligned with JSON-RPC 2.0 where one exists (``-32601`` method not
found, ``-32602`` invalid params, ``-32603`` internal) and use a Kinora-private
range (``-32000…-32099``, the JSON-RPC "implementation-defined server error"
band) for the domain categories (auth, budget, not-found, conflict, …). Nothing
here renders or spends credits — it is pure protocol vocabulary.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class ErrorCategory(StrEnum):
    """The machine-readable failure category a client branches on.

    Categories map many-to-one onto JSON-RPC numeric codes; the category is the
    *semantic* axis (what went wrong) and the code is the *wire* axis (how a
    spec-compliant JSON-RPC client classifies it).
    """

    #: The requested tool / method does not exist (JSON-RPC method-not-found).
    METHOD_NOT_FOUND = "method_not_found"
    #: Arguments failed JSON-Schema validation (JSON-RPC invalid-params).
    INVALID_PARAMS = "invalid_params"
    #: The server produced a payload that did not match its declared output schema.
    INVALID_RESPONSE = "invalid_response"
    #: The caller is not permitted (bad/forged book_id, missing scope, …).
    UNAUTHORIZED = "unauthorized"
    #: A referenced object does not exist (book / shot / job / branch).
    NOT_FOUND = "not_found"
    #: A version constraint could not be satisfied (unknown / yanked tool version).
    VERSION = "version"
    #: A guardrail refused the call (budget ceiling, live-video gate).
    GUARDRAIL = "guardrail"
    #: A write conflicted with concurrent state (CRDT merge loser, etc.).
    CONFLICT = "conflict"
    #: The transport / capability negotiation rejected the request.
    PROTOCOL = "protocol"
    #: An unexpected server-side failure (JSON-RPC internal error).
    INTERNAL = "internal"


#: Category -> the JSON-RPC numeric code a spec-compliant client sees.
_CATEGORY_CODE: dict[ErrorCategory, int] = {
    ErrorCategory.METHOD_NOT_FOUND: -32601,
    ErrorCategory.INVALID_PARAMS: -32602,
    ErrorCategory.INTERNAL: -32603,
    ErrorCategory.INVALID_RESPONSE: -32001,
    ErrorCategory.UNAUTHORIZED: -32002,
    ErrorCategory.NOT_FOUND: -32003,
    ErrorCategory.VERSION: -32004,
    ErrorCategory.GUARDRAIL: -32005,
    ErrorCategory.CONFLICT: -32006,
    ErrorCategory.PROTOCOL: -32007,
}


def code_for(category: ErrorCategory) -> int:
    """The JSON-RPC numeric code for an :class:`ErrorCategory`."""
    return _CATEGORY_CODE[category]


@dataclass(frozen=True, slots=True)
class MCPErrorBody:
    """The serializable payload of an MCP error — the shape clients receive.

    Carried both inside an ``isError`` tool result (as structured content) and,
    for protocol-level failures, as the JSON-RPC ``error`` object.
    """

    category: ErrorCategory
    message: str
    code: int
    data: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        """A plain JSON-serializable dict (the wire form)."""
        body: dict[str, Any] = {
            "error": True,
            "category": self.category.value,
            "code": self.code,
            "message": self.message,
        }
        if self.data:
            body["data"] = self.data
        return body


class MCPError(Exception):
    """Base class for every typed MCP failure raised inside the protocol layer.

    Subclasses fix the :class:`ErrorCategory`; the numeric ``code`` is derived
    from it. ``data`` carries machine-readable detail (the offending tool name,
    the schema path, the missing scope, …).
    """

    category: ErrorCategory = ErrorCategory.INTERNAL

    def __init__(self, message: str, *, data: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.data = data

    @property
    def code(self) -> int:
        """The JSON-RPC numeric code for this error's category."""
        return code_for(self.category)

    def body(self) -> MCPErrorBody:
        """The serializable :class:`MCPErrorBody`."""
        return MCPErrorBody(
            category=self.category, message=self.message, code=self.code, data=self.data
        )

    def to_dict(self) -> dict[str, Any]:
        """Shortcut for ``self.body().to_dict()``."""
        return self.body().to_dict()


class MethodNotFoundError(MCPError):
    """The named tool / method is not registered."""

    category = ErrorCategory.METHOD_NOT_FOUND


class InvalidParamsError(MCPError):
    """Request arguments failed schema validation."""

    category = ErrorCategory.INVALID_PARAMS


class InvalidResponseError(MCPError):
    """The handler returned a payload that did not match its output schema."""

    category = ErrorCategory.INVALID_RESPONSE


class UnauthorizedError(MCPError):
    """The caller is not permitted to perform this call."""

    category = ErrorCategory.UNAUTHORIZED


class NotFoundError(MCPError):
    """A referenced resource / object does not exist."""

    category = ErrorCategory.NOT_FOUND


class VersionError(MCPError):
    """A requested tool version could not be satisfied."""

    category = ErrorCategory.VERSION


class GuardrailError(MCPError):
    """A guardrail (budget / live-gate) refused the call."""

    category = ErrorCategory.GUARDRAIL


class ConflictError(MCPError):
    """A write conflicted with concurrent state."""

    category = ErrorCategory.CONFLICT


class ProtocolError(MCPError):
    """A transport / capability-negotiation rejection."""

    category = ErrorCategory.PROTOCOL


def to_error_body(exc: BaseException) -> MCPErrorBody:
    """Coerce any exception into a typed :class:`MCPErrorBody`.

    Known :class:`MCPError` subclasses pass their category through. The
    book-scoped authorizer raises :class:`app.mcp.authz.MCPAuthorizationError`
    (an unauthorized denial) — recognised by name to avoid an import cycle.
    Everything else becomes an :data:`ErrorCategory.INTERNAL` body, never
    leaking a stack trace into the wire payload.
    """
    if isinstance(exc, MCPError):
        return exc.body()
    name = type(exc).__name__
    if name == "MCPAuthorizationError":
        return MCPErrorBody(
            category=ErrorCategory.UNAUTHORIZED,
            message=str(exc),
            code=code_for(ErrorCategory.UNAUTHORIZED),
        )
    if name == "BudgetExceeded":
        scope = getattr(exc, "scope", None)
        return MCPErrorBody(
            category=ErrorCategory.GUARDRAIL,
            message=str(exc),
            code=code_for(ErrorCategory.GUARDRAIL),
            data={"scope": scope} if scope else None,
        )
    if name in {"ValidationError", "ValueError"}:
        return MCPErrorBody(
            category=ErrorCategory.INVALID_PARAMS,
            message=str(exc),
            code=code_for(ErrorCategory.INVALID_PARAMS),
        )
    return MCPErrorBody(
        category=ErrorCategory.INTERNAL,
        message=f"{name}: {exc}",
        code=code_for(ErrorCategory.INTERNAL),
    )


__all__ = [
    "ConflictError",
    "ErrorCategory",
    "GuardrailError",
    "InvalidParamsError",
    "InvalidResponseError",
    "MCPError",
    "MCPErrorBody",
    "MethodNotFoundError",
    "NotFoundError",
    "ProtocolError",
    "UnauthorizedError",
    "VersionError",
    "code_for",
    "to_error_body",
]
