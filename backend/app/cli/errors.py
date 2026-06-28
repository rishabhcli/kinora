"""CLI error types — exit-code-carrying exceptions for the command shells.

The action functions raise :class:`CliError` for *expected* operator-facing
failures (a missing book, a bad argument). The top-level runner catches it,
prints the message to stderr, and exits with the carried code, so the CLI is
script-friendly (distinct exit codes) without leaking tracebacks for routine
"not found" cases. Unexpected exceptions still bubble up as tracebacks.
"""

from __future__ import annotations

# Conventional exit codes (sysexits-ish, kept small and stable).
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_NOT_FOUND = 3
EXIT_CONFLICT = 4
EXIT_UNAVAILABLE = 5


class CliError(RuntimeError):
    """An expected, operator-facing CLI failure carrying a process exit code."""

    def __init__(self, message: str, *, exit_code: int = EXIT_ERROR) -> None:
        super().__init__(message)
        self.exit_code = exit_code


def not_found(what: str, ident: str) -> CliError:
    """A uniform "X 'id' not found" error (exit code 3)."""
    return CliError(f"{what} not found: {ident}", exit_code=EXIT_NOT_FOUND)


def usage(message: str) -> CliError:
    """A bad-argument / misuse error (exit code 2)."""
    return CliError(message, exit_code=EXIT_USAGE)


def conflict(message: str) -> CliError:
    """A precondition / state-conflict error (exit code 4)."""
    return CliError(message, exit_code=EXIT_CONFLICT)


def unavailable(message: str) -> CliError:
    """A dependency-unreachable error (exit code 5)."""
    return CliError(message, exit_code=EXIT_UNAVAILABLE)


__all__ = [
    "EXIT_CONFLICT",
    "EXIT_ERROR",
    "EXIT_NOT_FOUND",
    "EXIT_OK",
    "EXIT_UNAVAILABLE",
    "EXIT_USAGE",
    "CliError",
    "conflict",
    "not_found",
    "unavailable",
    "usage",
]
