"""Domain-layer errors for the write side.

These are *business-rule* failures the decision functions raise — distinct from
the infrastructure :class:`~app.eventsourcing.store.ConcurrencyError`, which the
command bus handles by retrying. A :class:`DomainError` means the command is
invalid against the current aggregate state and retrying would not help, so the
bus surfaces it to the caller.
"""

from __future__ import annotations


class DomainError(Exception):
    """Base class for write-side business-rule violations."""


class InvariantViolation(DomainError):  # noqa: N818 - domain-vocabulary name
    """A command would break an aggregate invariant (e.g. an illegal transition)."""


class CommandRejected(DomainError):  # noqa: N818 - domain-vocabulary name
    """A command is structurally valid but not applicable to the current state."""


class AggregateNotFound(DomainError):  # noqa: N818 - domain-vocabulary name
    """A command targeted an aggregate id whose stream has no events yet."""

    def __init__(self, stream_id: str) -> None:
        self.stream_id = stream_id
        super().__init__(f"no such aggregate: {stream_id}")


class ValidationError(DomainError):
    """A command failed structural validation in the validation middleware."""

    def __init__(self, message: str, *, field: str | None = None) -> None:
        self.field = field
        super().__init__(message)


class AuthorizationError(DomainError):
    """A command was rejected by the auth middleware seam."""


__all__ = [
    "AggregateNotFound",
    "AuthorizationError",
    "CommandRejected",
    "DomainError",
    "InvariantViolation",
    "ValidationError",
]
