"""Request context — the metadata every RPC carries and propagates.

A :class:`RequestContext` is the per-call envelope of *cross-cutting* state that
must follow a request across every service hop: the **deadline** (time budget,
§4.8 cancellation), the **trace / correlation** ids (so one render's logs and
spans stitch together — bridged to :mod:`app.telemetry.context`), the **auth**
principal + token, the **tenant** (the reader / workspace the work is for), an
**idempotency key** (so a duplicate Scheduler event can't double-spend the video
budget, §12.1), and free-form **baggage** (small key/values that ride along, e.g.
``session_id`` / ``shot_hash``).

The context lives in a :class:`contextvars.ContextVar`, so it propagates
automatically across ``await`` boundaries and into child tasks without any call
site threading it through — the same trick :mod:`app.telemetry.context` uses for
correlation ids. A server that receives a wire request **rehydrates** a context
from headers; a client **serializes** the current context into headers before a
hop. :meth:`RequestContext.child` derives the downstream context: it keeps the
trace, *inherits the shrinking deadline*, and opens a fresh span — exactly the
W3C trace-context propagation a real mesh does.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator, Mapping
from contextvars import ContextVar, Token
from dataclasses import dataclass, field, replace
from typing import Any

from app.distributed.rpc.deadline import Clock, Deadline
from app.telemetry.context import (
    bind_correlation_id,
    new_correlation_id,
    new_span_id,
    new_trace_id,
    reset_context,
)

# --------------------------------------------------------------------------- #
# Header names — the wire keys a transport reads/writes. W3C-ish where one
# exists; ``x-kinora-*`` for the bespoke fields. Stable across transports.
# --------------------------------------------------------------------------- #

HEADER_TRACE_ID = "x-kinora-trace-id"
HEADER_SPAN_ID = "x-kinora-span-id"
HEADER_PARENT_SPAN_ID = "x-kinora-parent-span-id"
HEADER_CORRELATION_ID = "x-kinora-correlation-id"
HEADER_DEADLINE_MS = "x-kinora-deadline-ms"  # remaining budget at send time
HEADER_AUTH_PRINCIPAL = "x-kinora-principal"
HEADER_AUTH_TOKEN = "authorization"
HEADER_TENANT = "x-kinora-tenant"
HEADER_IDEMPOTENCY_KEY = "x-kinora-idempotency-key"
HEADER_BAGGAGE_PREFIX = "x-kinora-baggage-"


@dataclass(frozen=True, slots=True)
class AuthContext:
    """The authenticated principal + bearer token for a call.

    ``principal`` is the stable subject id (a user id, an agent id, or
    ``"system"`` for internal work); ``token`` is the bearer credential the
    transport forwards. ``scopes`` are coarse capability tags a server may check.
    """

    principal: str | None = None
    token: str | None = None
    scopes: tuple[str, ...] = ()

    @property
    def is_authenticated(self) -> bool:
        """True when a principal is present."""
        return self.principal is not None

    def has_scope(self, scope: str) -> bool:
        """Whether ``scope`` is granted (used by a server-side authz check)."""
        return scope in self.scopes


@dataclass(frozen=True, slots=True)
class RequestContext:
    """The immutable per-call envelope propagated across service hops.

    Construct a root with :meth:`root`; derive the next hop's context with
    :meth:`child`. Read the time budget with :meth:`remaining`.
    """

    deadline: Deadline = field(default_factory=Deadline.never)
    trace_id: str | None = None
    span_id: str | None = None
    parent_span_id: str | None = None
    correlation_id: str | None = None
    auth: AuthContext = field(default_factory=AuthContext)
    tenant: str | None = None
    idempotency_key: str | None = None
    baggage: Mapping[str, str] = field(default_factory=dict)
    #: Hop counter — incremented on each :meth:`child` so a runaway fan-out
    #: (a cycle, a mis-wired façade) can be cut off by a max-depth guard.
    depth: int = 0

    # -- construction ------------------------------------------------------- #

    @classmethod
    def root(
        cls,
        *,
        clock: Clock,
        timeout_s: float | None = None,
        principal: str | None = None,
        token: str | None = None,
        scopes: tuple[str, ...] = (),
        tenant: str | None = None,
        idempotency_key: str | None = None,
        baggage: Mapping[str, str] | None = None,
        trace_id: str | None = None,
        correlation_id: str | None = None,
    ) -> RequestContext:
        """Start a fresh root context (a new trace + span).

        ``timeout_s`` sets the end-to-end deadline (``None`` => unbounded). A
        missing ``trace_id`` / ``correlation_id`` is generated, so the very first
        hop is always traceable.
        """
        deadline = (
            Deadline.after(timeout_s, clock=clock) if timeout_s is not None else Deadline.never()
        )
        return cls(
            deadline=deadline,
            trace_id=trace_id or new_trace_id(),
            span_id=new_span_id(),
            parent_span_id=None,
            correlation_id=correlation_id or new_correlation_id(),
            auth=AuthContext(principal=principal, token=token, scopes=scopes),
            tenant=tenant,
            idempotency_key=idempotency_key,
            baggage=dict(baggage or {}),
            depth=0,
        )

    def child(self) -> RequestContext:
        """Derive the downstream context for the next hop.

        Same trace, a new span whose parent is this span, the *same* (shrinking)
        deadline object (so the budget is shared, not reset), and ``depth + 1``.
        Auth / tenant / idempotency / baggage are carried through unchanged.
        """
        return replace(
            self,
            span_id=new_span_id(),
            parent_span_id=self.span_id,
            depth=self.depth + 1,
        )

    # -- mutators (return copies; the context is frozen) -------------------- #

    def with_deadline(self, deadline: Deadline) -> RequestContext:
        """Return a copy whose deadline is the tighter of the two."""
        return replace(self, deadline=self.deadline.min_with(deadline))

    def with_auth(self, auth: AuthContext) -> RequestContext:
        """Return a copy with a different auth principal/token."""
        return replace(self, auth=auth)

    def with_tenant(self, tenant: str | None) -> RequestContext:
        """Return a copy scoped to a different tenant."""
        return replace(self, tenant=tenant)

    def with_idempotency_key(self, key: str | None) -> RequestContext:
        """Return a copy carrying an idempotency key (dedup at the server)."""
        return replace(self, idempotency_key=key)

    def with_baggage(self, **items: str) -> RequestContext:
        """Return a copy with extra baggage items merged in."""
        merged = dict(self.baggage)
        merged.update(items)
        return replace(self, baggage=merged)

    # -- reads -------------------------------------------------------------- #

    def remaining(self, *, clock: Clock) -> float:
        """Seconds of budget left (``inf`` if unbounded)."""
        return self.deadline.remaining(clock=clock)

    def expired(self, *, clock: Clock) -> bool:
        """True once the deadline has passed (the call must stop)."""
        return self.deadline.expired(clock=clock)

    def baggage_get(self, key: str, default: str | None = None) -> str | None:
        """Read one baggage value."""
        return self.baggage.get(key, default)

    # -- wire serialization ------------------------------------------------- #

    def to_headers(self, *, clock: Clock) -> dict[str, str]:
        """Serialize the context into transport headers (the send side).

        The deadline is emitted as **remaining milliseconds** at send time (not
        an absolute instant) because the receiver's monotonic clock is a
        different origin — the receiver reconstructs an absolute deadline against
        *its own* clock from the remaining budget.
        """
        headers: dict[str, str] = {}
        if self.trace_id:
            headers[HEADER_TRACE_ID] = self.trace_id
        if self.span_id:
            headers[HEADER_SPAN_ID] = self.span_id
        if self.parent_span_id:
            headers[HEADER_PARENT_SPAN_ID] = self.parent_span_id
        if self.correlation_id:
            headers[HEADER_CORRELATION_ID] = self.correlation_id
        rem = self.deadline.remaining(clock=clock)
        if rem != float("inf"):
            headers[HEADER_DEADLINE_MS] = str(int(rem * 1000.0))
        if self.auth.principal:
            headers[HEADER_AUTH_PRINCIPAL] = self.auth.principal
        if self.auth.token:
            headers[HEADER_AUTH_TOKEN] = f"Bearer {self.auth.token}"
        if self.tenant:
            headers[HEADER_TENANT] = self.tenant
        if self.idempotency_key:
            headers[HEADER_IDEMPOTENCY_KEY] = self.idempotency_key
        for key, value in self.baggage.items():
            headers[HEADER_BAGGAGE_PREFIX + key] = value
        return headers

    @classmethod
    def from_headers(cls, headers: Mapping[str, str], *, clock: Clock) -> RequestContext:
        """Rehydrate a context from transport headers (the receive side).

        Header names are matched case-insensitively. The remaining-ms deadline is
        reconstructed against the *receiver's* clock. A missing trace/correlation
        id is generated so an un-instrumented caller still produces a traceable
        server-side context. The receiver always opens a fresh span whose parent
        is the caller's span id.
        """
        lower = {k.lower(): v for k, v in headers.items()}

        def _get(name: str) -> str | None:
            return lower.get(name.lower())

        deadline = Deadline.never()
        raw_deadline = _get(HEADER_DEADLINE_MS)
        if raw_deadline is not None:
            with contextlib.suppress(ValueError):
                deadline = Deadline.after(max(0.0, int(raw_deadline) / 1000.0), clock=clock)

        token = _get(HEADER_AUTH_TOKEN)
        if token and token.lower().startswith("bearer "):
            token = token[7:]

        baggage = {
            k[len(HEADER_BAGGAGE_PREFIX) :]: v
            for k, v in lower.items()
            if k.startswith(HEADER_BAGGAGE_PREFIX)
        }

        caller_span = _get(HEADER_SPAN_ID)
        return cls(
            deadline=deadline,
            trace_id=_get(HEADER_TRACE_ID) or new_trace_id(),
            span_id=new_span_id(),
            parent_span_id=caller_span,
            correlation_id=_get(HEADER_CORRELATION_ID) or new_correlation_id(),
            auth=AuthContext(principal=_get(HEADER_AUTH_PRINCIPAL), token=token),
            tenant=_get(HEADER_TENANT),
            idempotency_key=_get(HEADER_IDEMPOTENCY_KEY),
            baggage=baggage,
            depth=0,
        )


# --------------------------------------------------------------------------- #
# Ambient context (contextvars) — propagates across await + child tasks.
# --------------------------------------------------------------------------- #

_current: ContextVar[RequestContext | None] = ContextVar("kinora_rpc_context", default=None)


def current_context() -> RequestContext | None:
    """Return the ambient :class:`RequestContext` (``None`` outside any scope)."""
    return _current.get()


def bind_context(ctx: RequestContext) -> Token[RequestContext | None]:
    """Bind ``ctx`` as the ambient context; returns a reset token.

    Also mirrors the trace/correlation ids into :mod:`app.telemetry.context` so
    structlog log lines emitted under this scope carry the same ids — the RPC
    layer and the telemetry layer agree on one trace.
    """
    token = _current.set(ctx)
    bind_correlation_id(
        ctx.correlation_id,
        trace_id=ctx.trace_id,
        span_id=ctx.span_id,
    )
    return token


def reset_token(token: Token[RequestContext | None]) -> None:
    """Restore the ambient context from a :func:`bind_context` token."""
    with contextlib.suppress(ValueError):
        _current.reset(token)


@contextlib.contextmanager
def context_scope(ctx: RequestContext) -> Iterator[RequestContext]:
    """Scope ``ctx`` as the ambient context for a block, restoring on exit.

    Mirrors the telemetry correlation scope so nested RPC scopes never leak ids
    into a sibling request.
    """
    prev_tele = bind_correlation_id(
        ctx.correlation_id,
        trace_id=ctx.trace_id,
        span_id=ctx.span_id,
    )
    token = _current.set(ctx)
    try:
        yield ctx
    finally:
        with contextlib.suppress(ValueError):
            _current.reset(token)
        reset_context(prev_tele)


def require_context() -> RequestContext:
    """Return the ambient context or raise — for code that must run under one."""
    ctx = _current.get()
    if ctx is None:
        raise RuntimeError("no RequestContext bound; call inside context_scope()")
    return ctx


def context_baggage(**items: Any) -> dict[str, str]:
    """Coerce arbitrary kwargs to string baggage (drops ``None`` values)."""
    return {k: str(v) for k, v in items.items() if v is not None}


__all__ = [
    "HEADER_AUTH_PRINCIPAL",
    "HEADER_AUTH_TOKEN",
    "HEADER_BAGGAGE_PREFIX",
    "HEADER_CORRELATION_ID",
    "HEADER_DEADLINE_MS",
    "HEADER_IDEMPOTENCY_KEY",
    "HEADER_PARENT_SPAN_ID",
    "HEADER_SPAN_ID",
    "HEADER_TENANT",
    "HEADER_TRACE_ID",
    "AuthContext",
    "RequestContext",
    "bind_context",
    "context_baggage",
    "context_scope",
    "current_context",
    "require_context",
    "reset_token",
]
