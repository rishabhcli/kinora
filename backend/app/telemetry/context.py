"""Correlation / trace context — the spine of Kinora's contextual telemetry.

Every unit of work (an HTTP request, a render job, a crew negotiation) carries a
**correlation id**: a short stable token that ties together every log line, span,
and metric emitted while serving it. Distributed-trace ids (``trace_id`` /
``span_id``) ride alongside so a log line can be cross-referenced with a span in
a tracing UI even when the OpenTelemetry SDK is absent.

These three values live in :mod:`contextvars`, so they propagate automatically
across ``await`` boundaries and into child tasks without any call site having to
thread them through. A single structlog processor (:func:`merge_correlation`)
injects whatever is bound into every log event, so contextual logging is free at
every emit point.

Design rules:

* **W3C-shaped ids.** ``trace_id`` is 32 lowercase hex chars, ``span_id`` 16 —
  the same shape OpenTelemetry uses — so the pure-Python tracer and a real OTel
  span are interchangeable on the wire (see :mod:`app.telemetry.spans`).
* **Cheap + dependency-free.** Generating an id is one ``os.urandom`` read; no
  network, no SDK, safe to call on every request.
* **Bind/restore is a token.** :func:`bind_correlation_id` returns a token bundle
  you can pass to :func:`reset_context` (or use the :func:`correlation_scope`
  context manager) so nested scopes restore cleanly.
"""

from __future__ import annotations

import contextlib
import os
import uuid
from collections.abc import Iterator
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Any

from structlog.typing import EventDict, WrappedLogger

# --------------------------------------------------------------------------- #
# The context variables (propagate across await + child tasks).
# --------------------------------------------------------------------------- #

#: The application-level correlation id (one per request / job / negotiation).
_correlation_id: ContextVar[str | None] = ContextVar("kinora_correlation_id", default=None)
#: The active distributed-trace id (32 hex chars; shared by a whole trace).
_trace_id: ContextVar[str | None] = ContextVar("kinora_trace_id", default=None)
#: The active span id (16 hex chars; the innermost open span).
_span_id: ContextVar[str | None] = ContextVar("kinora_span_id", default=None)

#: The log keys the correlation values are surfaced under.
CORRELATION_KEY = "correlation_id"
TRACE_KEY = "trace_id"
SPAN_KEY = "span_id"


@dataclass(frozen=True, slots=True)
class ContextTokens:
    """Reset tokens for a :func:`bind_correlation_id` call (pass to ``reset``)."""

    correlation: Token[str | None] | None = None
    trace: Token[str | None] | None = None
    span: Token[str | None] | None = None


# --------------------------------------------------------------------------- #
# Id generation (W3C-shaped, dependency-free).
# --------------------------------------------------------------------------- #


def new_correlation_id() -> str:
    """Return a fresh, human-skimmable correlation id (``corr_<12 hex>``)."""
    return "corr_" + uuid.uuid4().hex[:12]


def new_trace_id() -> str:
    """Return a fresh W3C-shaped trace id (32 lowercase hex chars, non-zero)."""
    raw = os.urandom(16)
    value = raw.hex()
    # The all-zero trace id is invalid per the W3C trace-context spec.
    return value if int(value, 16) != 0 else (os.urandom(15) + b"\x01").hex()


def new_span_id() -> str:
    """Return a fresh W3C-shaped span id (16 lowercase hex chars, non-zero)."""
    raw = os.urandom(8)
    value = raw.hex()
    return value if int(value, 16) != 0 else (os.urandom(7) + b"\x01").hex()


# --------------------------------------------------------------------------- #
# Readers.
# --------------------------------------------------------------------------- #


def get_correlation_id() -> str | None:
    """Return the bound correlation id (``None`` outside any scope)."""
    return _correlation_id.get()


def get_trace_id() -> str | None:
    """Return the active trace id (``None`` when no trace is open)."""
    return _trace_id.get()


def get_span_id() -> str | None:
    """Return the active span id (``None`` when no span is open)."""
    return _span_id.get()


def current_context() -> dict[str, str]:
    """Return the non-``None`` correlation/trace/span values as a flat dict."""
    out: dict[str, str] = {}
    cid = _correlation_id.get()
    tid = _trace_id.get()
    sid = _span_id.get()
    if cid is not None:
        out[CORRELATION_KEY] = cid
    if tid is not None:
        out[TRACE_KEY] = tid
    if sid is not None:
        out[SPAN_KEY] = sid
    return out


# --------------------------------------------------------------------------- #
# Binders.
# --------------------------------------------------------------------------- #


def bind_correlation_id(
    correlation_id: str | None = None,
    *,
    trace_id: str | None = None,
    span_id: str | None = None,
) -> ContextTokens:
    """Bind correlation/trace/span ids onto the current context.

    A missing ``correlation_id`` is generated. ``trace_id`` / ``span_id`` are set
    only when provided (a span open later will set them itself). Returns the reset
    tokens; pass them to :func:`reset_context` to restore the prior values.
    """
    corr = correlation_id or new_correlation_id()
    ctok = _correlation_id.set(corr)
    ttok = _trace_id.set(trace_id) if trace_id is not None else None
    stok = _span_id.set(span_id) if span_id is not None else None
    return ContextTokens(correlation=ctok, trace=ttok, span=stok)


def set_trace_context(trace_id: str, span_id: str) -> ContextTokens:
    """Set the active trace + span ids (used by the tracer when a span opens)."""
    ttok = _trace_id.set(trace_id)
    stok = _span_id.set(span_id)
    return ContextTokens(trace=ttok, span=stok)


def set_span_id(span_id: str | None) -> Token[str | None]:
    """Set just the active span id; returns its reset token."""
    return _span_id.set(span_id)


def reset_context(tokens: ContextTokens) -> None:
    """Restore the context vars from a :class:`ContextTokens` bundle."""
    if tokens.span is not None:
        with contextlib.suppress(ValueError):
            _span_id.reset(tokens.span)
    if tokens.trace is not None:
        with contextlib.suppress(ValueError):
            _trace_id.reset(tokens.trace)
    if tokens.correlation is not None:
        with contextlib.suppress(ValueError):
            _correlation_id.reset(tokens.correlation)


@contextlib.contextmanager
def correlation_scope(
    correlation_id: str | None = None,
    *,
    trace_id: str | None = None,
    span_id: str | None = None,
) -> Iterator[str]:
    """Scope a correlation id (and optional trace/span) for a block of work.

    Yields the bound correlation id and restores the prior context on exit, so
    nested scopes never leak ids into a sibling request.
    """
    tokens = bind_correlation_id(correlation_id, trace_id=trace_id, span_id=span_id)
    try:
        yield _correlation_id.get() or ""
    finally:
        reset_context(tokens)


def clear_context() -> None:
    """Drop all correlation/trace/span values (mainly for tests)."""
    _correlation_id.set(None)
    _trace_id.set(None)
    _span_id.set(None)


# --------------------------------------------------------------------------- #
# structlog processor.
# --------------------------------------------------------------------------- #


def merge_correlation(_logger: WrappedLogger, _name: str, event_dict: EventDict) -> EventDict:
    """structlog processor: inject the bound correlation/trace/span ids.

    Add this to the processor chain (before the renderer) and every log line
    automatically carries ``correlation_id`` / ``trace_id`` / ``span_id`` when a
    scope is active. Values already present on the event are *not* overwritten, so
    an explicit ``log.info(..., correlation_id=...)`` still wins.
    """
    ctx = current_context()
    for key, value in ctx.items():
        event_dict.setdefault(key, value)
    return event_dict


def context_logging_processors() -> list[Any]:
    """Return the processor list to splice into the structlog chain.

    Kept as a list so the logging module can extend its shared chain additively
    without importing this module's internals.
    """
    return [merge_correlation]


__all__ = [
    "CORRELATION_KEY",
    "SPAN_KEY",
    "TRACE_KEY",
    "ContextTokens",
    "bind_correlation_id",
    "clear_context",
    "context_logging_processors",
    "correlation_scope",
    "current_context",
    "get_correlation_id",
    "get_span_id",
    "get_trace_id",
    "merge_correlation",
    "new_correlation_id",
    "new_span_id",
    "new_trace_id",
    "reset_context",
    "set_span_id",
    "set_trace_context",
]
