"""Structured-log enrichment — domain ids layered onto structlog (§12).

The correlation / trace / span ids already propagate through
:mod:`app.telemetry.context` (a structlog processor injects them into every log
line). This module adds the **Kinora-domain dimension** on top: the
``book_id`` / ``session_id`` / ``shot_id`` / ``provider`` / ``render_state`` a
unit of work is operating on. Those values ride the same
:mod:`contextvars` spine, so once bound at the top of a render they appear on
*every* log line and span emitted underneath — without any call site threading
them through.

Design rules (mirroring :mod:`app.telemetry.context`):

* **Contextvars, not globals.** Values propagate across ``await`` boundaries and
  into child tasks automatically; a sibling request never sees another's ids.
* **Bind/restore is a token.** :func:`bind_render_context` returns a token bundle
  you pass to :func:`reset_render_context` (or use the :func:`render_log_context`
  context manager) so nested scopes restore cleanly.
* **Set-only-when-given.** Binding ``None`` for a field leaves the inherited value
  in place, so a provider span nested in a shot render keeps the shot id.
* **Explicit wins.** The structlog processor uses ``setdefault`` so an explicit
  ``log.info(..., shot_id=...)`` is never overwritten.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Any

from structlog.typing import EventDict, WrappedLogger

# --------------------------------------------------------------------------- #
# The domain context variables (propagate across await + child tasks).
# --------------------------------------------------------------------------- #

_book_id: ContextVar[str | None] = ContextVar("kinora_book_id", default=None)
_session_id: ContextVar[str | None] = ContextVar("kinora_session_id", default=None)
_shot_id: ContextVar[str | None] = ContextVar("kinora_shot_id", default=None)
_provider: ContextVar[str | None] = ContextVar("kinora_provider", default=None)
_render_state: ContextVar[str | None] = ContextVar("kinora_render_state", default=None)

#: The log keys (and span-attribute keys) each value is surfaced under. Ordered
#: so a rendered log line reads book → session → shot → provider → state.
BOOK_KEY = "book_id"
SESSION_KEY = "session_id"
SHOT_KEY = "shot_id"
PROVIDER_KEY = "provider"
RENDER_STATE_KEY = "render_state"

_VARS: tuple[tuple[str, ContextVar[str | None]], ...] = (
    (BOOK_KEY, _book_id),
    (SESSION_KEY, _session_id),
    (SHOT_KEY, _shot_id),
    (PROVIDER_KEY, _provider),
    (RENDER_STATE_KEY, _render_state),
)


@dataclass(frozen=True, slots=True)
class RenderContextTokens:
    """Reset tokens for a :func:`bind_render_context` call (pass to ``reset``)."""

    book: Token[str | None] | None = None
    session: Token[str | None] | None = None
    shot: Token[str | None] | None = None
    provider: Token[str | None] | None = None
    render_state: Token[str | None] | None = None


# --------------------------------------------------------------------------- #
# Readers.
# --------------------------------------------------------------------------- #


def get_book_id() -> str | None:
    """Return the bound book id (``None`` outside any render scope)."""
    return _book_id.get()


def get_session_id() -> str | None:
    """Return the bound reading-session id (``None`` outside any scope)."""
    return _session_id.get()


def get_shot_id() -> str | None:
    """Return the bound shot id (``None`` outside a per-shot scope)."""
    return _shot_id.get()


def get_provider() -> str | None:
    """Return the bound provider/model (``None`` outside a provider scope)."""
    return _provider.get()


def get_render_state() -> str | None:
    """Return the bound §9.7 render-state name (``None`` outside a render)."""
    return _render_state.get()


def current_render_context() -> dict[str, str]:
    """Return the non-``None`` domain ids as a flat dict (for spans/logs)."""
    out: dict[str, str] = {}
    for key, var in _VARS:
        value = var.get()
        if value is not None:
            out[key] = value
    return out


# --------------------------------------------------------------------------- #
# Binders.
# --------------------------------------------------------------------------- #


def bind_render_context(
    *,
    book_id: str | None = None,
    session_id: str | None = None,
    shot_id: str | None = None,
    provider: str | None = None,
    render_state: str | None = None,
) -> RenderContextTokens:
    """Bind any subset of the domain ids onto the current context.

    A field left ``None`` is *not* touched — so binding a ``shot_id`` deeper in a
    render keeps the inherited ``book_id`` / ``session_id``. Returns the reset
    tokens; pass them to :func:`reset_render_context` (or prefer the
    :func:`render_log_context` context manager).
    """
    return RenderContextTokens(
        book=_book_id.set(book_id) if book_id is not None else None,
        session=_session_id.set(session_id) if session_id is not None else None,
        shot=_shot_id.set(shot_id) if shot_id is not None else None,
        provider=_provider.set(provider) if provider is not None else None,
        render_state=_render_state.set(render_state) if render_state is not None else None,
    )


def reset_render_context(tokens: RenderContextTokens) -> None:
    """Restore the domain context vars from a :class:`RenderContextTokens` bundle."""
    # Reset in reverse bind order; suppress ValueError so resetting a token from a
    # different context (e.g. after a task hop) degrades to a no-op.
    for token, var in (
        (tokens.render_state, _render_state),
        (tokens.provider, _provider),
        (tokens.shot, _shot_id),
        (tokens.session, _session_id),
        (tokens.book, _book_id),
    ):
        if token is not None:
            with contextlib.suppress(ValueError):
                var.reset(token)


@contextlib.contextmanager
def render_log_context(
    *,
    book_id: str | None = None,
    session_id: str | None = None,
    shot_id: str | None = None,
    provider: str | None = None,
    render_state: str | None = None,
) -> Iterator[dict[str, str]]:
    """Scope a set of domain ids for a block of work.

    Yields the merged context dict (the ids visible inside the block) and restores
    the prior values on exit so a nested scope never leaks ids into a sibling.
    """
    tokens = bind_render_context(
        book_id=book_id,
        session_id=session_id,
        shot_id=shot_id,
        provider=provider,
        render_state=render_state,
    )
    try:
        yield current_render_context()
    finally:
        reset_render_context(tokens)


def clear_render_context() -> None:
    """Drop every bound domain id (mainly for tests)."""
    for _key, var in _VARS:
        var.set(None)


# --------------------------------------------------------------------------- #
# structlog processor.
# --------------------------------------------------------------------------- #


def merge_render_context(_logger: WrappedLogger, _name: str, event_dict: EventDict) -> EventDict:
    """structlog processor: inject the bound domain ids into every log event.

    Add this to the processor chain (before the renderer) and every log line
    automatically carries ``book_id`` / ``session_id`` / ``shot_id`` /
    ``provider`` / ``render_state`` when bound. Values already present on the event
    are *not* overwritten, so an explicit binding at the call site still wins.
    """
    for key, value in current_render_context().items():
        event_dict.setdefault(key, value)
    return event_dict


def render_context_processors() -> list[Any]:
    """Return the processor list to splice into the structlog chain.

    Kept as a list (mirroring :func:`app.telemetry.context.context_logging_processors`)
    so the logging module can extend its shared chain additively without importing
    this module's internals.
    """
    return [merge_render_context]


__all__ = [
    "BOOK_KEY",
    "PROVIDER_KEY",
    "RENDER_STATE_KEY",
    "SESSION_KEY",
    "SHOT_KEY",
    "RenderContextTokens",
    "bind_render_context",
    "clear_render_context",
    "current_render_context",
    "get_book_id",
    "get_provider",
    "get_render_state",
    "get_session_id",
    "get_shot_id",
    "merge_render_context",
    "render_context_processors",
    "render_log_context",
    "reset_render_context",
]
