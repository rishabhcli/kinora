"""GraphQL error types + the gateway's error-masking policy.

A public API must never leak stack traces, secrets, or internal identifiers in
its error responses (mirrors the REST gateway's §12 scrubbing in
``app/api/errors.py``). :class:`GraphQLError` is the structured error every
failure becomes; :func:`mask_error` decides what an *external* consumer sees:

* errors explicitly raised as :class:`GraphQLError` (validation, auth, not-found,
  rate-limit, …) carry a safe ``message`` + machine-readable ``extensions.code``
  and pass through verbatim;
* any *unexpected* exception is collapsed to a generic ``INTERNAL_SERVER_ERROR``
  with a stable message, so an implementation bug can't exfiltrate internals.
"""

from __future__ import annotations

from typing import Any


class ErrorCode:
    """Stable machine-readable error codes carried in ``extensions.code``."""

    GRAPHQL_PARSE_FAILED = "GRAPHQL_PARSE_FAILED"
    GRAPHQL_VALIDATION_FAILED = "GRAPHQL_VALIDATION_FAILED"
    BAD_USER_INPUT = "BAD_USER_INPUT"
    UNAUTHENTICATED = "UNAUTHENTICATED"
    FORBIDDEN = "FORBIDDEN"
    NOT_FOUND = "NOT_FOUND"
    RATE_LIMITED = "RATE_LIMITED"
    PERSISTED_QUERY_NOT_FOUND = "PERSISTED_QUERY_NOT_FOUND"
    PERSISTED_QUERY_NOT_SUPPORTED = "PERSISTED_QUERY_NOT_SUPPORTED"
    DEPTH_LIMIT_EXCEEDED = "DEPTH_LIMIT_EXCEEDED"
    COMPLEXITY_LIMIT_EXCEEDED = "COMPLEXITY_LIMIT_EXCEEDED"
    OPERATION_NOT_ALLOWED = "OPERATION_NOT_ALLOWED"
    INTERNAL_SERVER_ERROR = "INTERNAL_SERVER_ERROR"


class GraphQLError(Exception):
    """A structured, externally-safe GraphQL error.

    ``path`` is the response path the error occurred at (set by the executor);
    ``locations`` are 1-based source positions; ``extensions`` is a free-form map
    that always carries ``code``.
    """

    def __init__(
        self,
        message: str,
        *,
        code: str = ErrorCode.INTERNAL_SERVER_ERROR,
        path: list[str | int] | None = None,
        locations: list[tuple[int, int]] | None = None,
        extensions: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.path = path
        self.locations = locations
        self.extensions = dict(extensions or {})
        self.extensions.setdefault("code", code)

    def with_path(self, path: list[str | int]) -> GraphQLError:
        """Return a copy of this error stamped with a response ``path``."""
        return GraphQLError(
            self.message,
            code=self.code,
            path=path,
            locations=self.locations,
            extensions=dict(self.extensions),
        )

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"message": self.message}
        if self.locations:
            out["locations"] = [{"line": ln, "column": col} for ln, col in self.locations]
        if self.path is not None:
            out["path"] = list(self.path)
        if self.extensions:
            out["extensions"] = self.extensions
        return out


def mask_error(exc: Exception, *, path: list[str | int] | None = None) -> GraphQLError:
    """Project any raised exception into an externally-safe :class:`GraphQLError`.

    A :class:`GraphQLError` is trusted and passes through (re-stamped with ``path``
    when one is supplied and it has none). Everything else is masked to a generic
    internal error — no class name, no message, no traceback escapes.
    """
    if isinstance(exc, GraphQLError):
        if path is not None and exc.path is None:
            return exc.with_path(path)
        return exc
    return GraphQLError(
        "Internal server error.",
        code=ErrorCode.INTERNAL_SERVER_ERROR,
        path=path,
    )


# -- convenience constructors ------------------------------------------------ #


def bad_input(message: str, **kw: Any) -> GraphQLError:
    return GraphQLError(message, code=ErrorCode.BAD_USER_INPUT, **kw)


def unauthenticated(message: str = "Authentication is required.") -> GraphQLError:
    return GraphQLError(message, code=ErrorCode.UNAUTHENTICATED)


def forbidden(message: str = "You do not have access to this resource.") -> GraphQLError:
    return GraphQLError(message, code=ErrorCode.FORBIDDEN)


def not_found(message: str = "Not found.") -> GraphQLError:
    return GraphQLError(message, code=ErrorCode.NOT_FOUND)


def rate_limited(message: str = "Rate limit exceeded.", **extensions: Any) -> GraphQLError:
    return GraphQLError(message, code=ErrorCode.RATE_LIMITED, extensions=extensions)


__all__ = [
    "ErrorCode",
    "GraphQLError",
    "bad_input",
    "forbidden",
    "mask_error",
    "not_found",
    "rate_limited",
    "unauthenticated",
]
