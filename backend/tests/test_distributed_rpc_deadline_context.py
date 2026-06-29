"""Tests for deadlines, the injectable clock, and request-context propagation."""

from __future__ import annotations

import pytest

from app.distributed.rpc.context import (
    HEADER_DEADLINE_MS,
    AuthContext,
    RequestContext,
    context_scope,
    current_context,
    require_context,
)
from app.distributed.rpc.deadline import (
    Deadline,
    ManualClock,
    deadline_for,
)

# -- Deadline / clock ------------------------------------------------------- #


def test_manual_clock_advances() -> None:
    clk = ManualClock()
    assert clk.now() == 0.0
    clk.advance(2.5)
    assert clk.now() == 2.5
    with pytest.raises(ValueError):
        clk.advance(-1.0)
    with pytest.raises(ValueError):
        clk.set(1.0)  # cannot go backwards


def test_deadline_after_and_remaining() -> None:
    clk = ManualClock()
    dl = Deadline.after(2.0, clock=clk)
    assert dl.remaining(clock=clk) == 2.0
    clk.advance(1.5)
    assert dl.remaining(clock=clk) == pytest.approx(0.5)
    clk.advance(1.0)
    assert dl.remaining(clock=clk) == 0.0
    assert dl.expired(clock=clk)


def test_deadline_never() -> None:
    clk = ManualClock()
    dl = Deadline.never()
    assert dl.is_infinite
    assert dl.remaining(clock=clk) == float("inf")
    clk.advance(1e9)
    assert not dl.expired(clock=clk)


def test_deadline_non_positive_is_already_expired() -> None:
    clk = ManualClock()
    dl = Deadline.after(0.0, clock=clk)
    assert dl.expired(clock=clk)
    dl2 = Deadline.after(-5.0, clock=clk)
    assert dl2.expired(clock=clk)


def test_deadline_min_with_takes_tighter() -> None:
    clk = ManualClock()
    near = Deadline.after(1.0, clock=clk)
    far = Deadline.after(5.0, clock=clk)
    assert near.min_with(far) is near
    assert far.min_with(near) is near


def test_deadline_for_inherits_and_shrinks() -> None:
    clk = ManualClock()
    inherited = Deadline.after(2.0, clock=clk)
    clk.advance(1.0)  # inherited now has 1.0 left
    # A fresh 5s timeout must be clamped to the inherited 1.0s remaining.
    effective = deadline_for(5.0, clock=clk, inherited=inherited)
    assert effective.remaining(clock=clk) == pytest.approx(1.0)


def test_deadline_for_no_timeout_no_inherited_is_infinite() -> None:
    clk = ManualClock()
    assert deadline_for(None, clock=clk).is_infinite


# -- RequestContext --------------------------------------------------------- #


def test_root_context_generates_ids() -> None:
    clk = ManualClock()
    ctx = RequestContext.root(clock=clk, timeout_s=3.0, principal="u1", tenant="t1")
    assert ctx.trace_id is not None and len(ctx.trace_id) == 32
    assert ctx.span_id is not None and len(ctx.span_id) == 16
    assert ctx.correlation_id is not None
    assert ctx.auth.principal == "u1"
    assert ctx.tenant == "t1"
    assert ctx.remaining(clock=clk) == 3.0
    assert ctx.depth == 0


def test_child_keeps_trace_new_span_inherits_deadline() -> None:
    clk = ManualClock()
    parent = RequestContext.root(clock=clk, timeout_s=2.0)
    clk.advance(0.5)
    child = parent.child()
    assert child.trace_id == parent.trace_id
    assert child.span_id != parent.span_id
    assert child.parent_span_id == parent.span_id
    assert child.depth == 1
    # The deadline is shared (shrinking), not reset.
    assert child.remaining(clock=clk) == pytest.approx(1.5)


def test_header_roundtrip_reconstructs_deadline_on_receiver_clock() -> None:
    send_clk = ManualClock()
    ctx = RequestContext.root(
        clock=send_clk,
        timeout_s=2.0,
        principal="reader",
        token="secret",
        tenant="ws",
        idempotency_key="shot#abc",
    ).with_baggage(session_id="sess-1")
    send_clk.advance(0.5)  # 1.5s remaining at send time
    headers = ctx.to_headers(clock=send_clk)
    assert headers[HEADER_DEADLINE_MS] == "1500"

    # Receiver has a totally different clock origin.
    recv_clk = ManualClock()
    recv_clk.set(100.0)
    rebuilt = RequestContext.from_headers(headers, clock=recv_clk)
    assert rebuilt.trace_id == ctx.trace_id
    assert rebuilt.parent_span_id == ctx.span_id  # receiver opens a fresh span
    assert rebuilt.span_id != ctx.span_id
    assert rebuilt.auth.principal == "reader"
    assert rebuilt.auth.token == "secret"  # "Bearer " stripped
    assert rebuilt.tenant == "ws"
    assert rebuilt.idempotency_key == "shot#abc"
    assert rebuilt.baggage_get("session_id") == "sess-1"
    assert rebuilt.remaining(clock=recv_clk) == pytest.approx(1.5)


def test_from_headers_generates_ids_for_uninstrumented_caller() -> None:
    clk = ManualClock()
    ctx = RequestContext.from_headers({}, clock=clk)
    assert ctx.trace_id is not None
    assert ctx.correlation_id is not None
    assert ctx.deadline.is_infinite


def test_with_mutators_return_copies() -> None:
    clk = ManualClock()
    ctx = RequestContext.root(clock=clk)
    ctx2 = ctx.with_auth(AuthContext(principal="x", scopes=("render",)))
    assert ctx.auth.principal is None
    assert ctx2.auth.has_scope("render")
    assert ctx2.with_tenant("t9").tenant == "t9"
    assert ctx2.with_idempotency_key("k").idempotency_key == "k"


def test_context_scope_binds_and_restores() -> None:
    clk = ManualClock()
    assert current_context() is None
    ctx = RequestContext.root(clock=clk, principal="u")
    with context_scope(ctx) as bound:
        assert bound is ctx
        assert current_context() is ctx
        assert require_context() is ctx
    assert current_context() is None


def test_require_context_raises_outside_scope() -> None:
    with pytest.raises(RuntimeError):
        require_context()
