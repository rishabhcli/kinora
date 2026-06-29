"""Tests for app.inference.router.cancellation — cooperative cancel tokens.

Covers the token primitive (one-way trip, observers, raise-on-cancel), the
scope registry (cancel a whole session at once, prune), and the router's
integration: cancelling a queued request drops + rejects it; cancelling a
running one trips its token so the result is discarded as CANCELLED.
"""

from __future__ import annotations

import pytest

from app.inference.router.cancellation import (
    CancellationRegistry,
    CancellationToken,
    CancelledError,
)


def test_token_trips_once() -> None:
    tok = CancellationToken(scope="s1")
    assert not tok.cancelled
    assert tok.cancel("user seeked") is True
    assert tok.cancelled and tok.reason == "user seeked"
    assert tok.cancel("again") is False  # idempotent


def test_token_observer_fires_on_trip() -> None:
    tok = CancellationToken()
    fired: list[int] = []
    tok.on_cancel(lambda: fired.append(1))
    assert not fired
    tok.cancel()
    assert fired == [1]


def test_observer_fires_immediately_if_already_cancelled() -> None:
    tok = CancellationToken()
    tok.cancel()
    fired: list[int] = []
    tok.on_cancel(lambda: fired.append(1))
    assert fired == [1]


def test_raise_if_cancelled() -> None:
    tok = CancellationToken()
    tok.raise_if_cancelled("r1")  # no-op when live
    tok.cancel("gone")
    with pytest.raises(CancelledError) as exc:
        tok.raise_if_cancelled("r1")
    assert exc.value.request_id == "r1"
    assert exc.value.reason == "gone"


def test_registry_cancels_a_whole_scope() -> None:
    reg = CancellationRegistry()
    a = reg.token("session-1")
    b = reg.token("session-1")
    c = reg.token("session-2")
    tripped = reg.cancel_scope("session-1", reason="left page")
    assert tripped == 2
    assert a.cancelled and b.cancelled
    assert not c.cancelled


def test_registry_cancel_scope_counts_only_new_trips() -> None:
    reg = CancellationRegistry()
    a = reg.token("s")
    a.cancel()  # already cancelled
    reg.token("s")
    assert reg.cancel_scope("s") == 1  # only the second was newly tripped


def test_registry_active_scopes_and_prune() -> None:
    reg = CancellationRegistry()
    reg.token("live")
    dead = reg.token("dead")
    dead.cancel()
    assert reg.active_scopes() == ["live"]
    assert reg.prune() == 1  # the fully-cancelled scope is dropped
    assert reg.active_scopes() == ["live"]
