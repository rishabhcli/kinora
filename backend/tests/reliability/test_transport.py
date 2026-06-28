"""Unit tests for the transport seam (app.reliability.transport).

These exercise *only* the FakeTransport — the unit suite never instantiates
HttpxTransport, so the test process opens no sockets (the brief's hard rule).
"""

from __future__ import annotations

import pytest

from app.reliability.transport import (
    FakeTransport,
    RecordedCall,
    Response,
    Transport,
    token_responder,
)


def test_response_ok_and_failure_flags() -> None:
    assert Response(status=200, elapsed_ms=1.0).ok is True
    assert Response(status=204, elapsed_ms=1.0).ok is True
    assert Response(status=503, elapsed_ms=1.0).ok is False
    assert Response(status=0, elapsed_ms=1.0, error="x").transport_failure is True
    assert Response(status=500, elapsed_ms=1.0).transport_failure is False


async def test_fake_transport_records_calls() -> None:
    t = FakeTransport(seed=1)
    await t.request("POST", "/sessions/s1/intent", json={"focus_word": 100})
    await t.request("GET", "/sessions/s1")
    assert len(t.calls) == 2
    assert isinstance(t.calls[0], RecordedCall)
    assert t.calls[0].method == "POST"
    assert t.calls[0].path == "/sessions/s1/intent"
    assert t.calls[0].json == {"focus_word": 100}
    assert t.calls_to("/sessions/s1") == [t.calls[1]]


async def test_fake_transport_is_a_transport() -> None:
    t = FakeTransport()
    assert isinstance(t, Transport)


async def test_fake_transport_default_response_is_healthy() -> None:
    t = FakeTransport(default_status=201, default_body={"id": "x"}, seed=2)
    resp = await t.request("POST", "/sessions")
    assert resp.status == 201
    assert resp.ok is True
    assert resp.body == {"id": "x"}
    assert resp.elapsed_ms >= 0.0


async def test_fake_transport_latency_deterministic() -> None:
    a = FakeTransport(base_latency_ms=50.0, latency_jitter_ms=10.0, seed=7)
    b = FakeTransport(base_latency_ms=50.0, latency_jitter_ms=10.0, seed=7)
    la = [(await a.request("GET", "/x")).elapsed_ms for _ in range(20)]
    lb = [(await b.request("GET", "/x")).elapsed_ms for _ in range(20)]
    assert la == lb
    assert all(x >= 0.0 for x in la)


async def test_fake_transport_zero_jitter_constant_latency() -> None:
    t = FakeTransport(base_latency_ms=12.0, latency_jitter_ms=0.0)
    resp = await t.request("GET", "/x")
    assert resp.elapsed_ms == 12.0


async def test_fake_transport_fault_injection_http() -> None:
    t = FakeTransport(fault_rate=1.0, fault_status=503, seed=3)
    resp = await t.request("POST", "/sessions/s/intent")
    assert resp.status == 503
    assert resp.ok is False
    assert resp.error is not None


async def test_fake_transport_fault_injection_transport_failure() -> None:
    t = FakeTransport(fault_rate=1.0, fault_status=0, seed=3)
    resp = await t.request("POST", "/x")
    assert resp.status == 0
    assert resp.transport_failure is True


async def test_fake_transport_partial_fault_rate() -> None:
    t = FakeTransport(fault_rate=0.3, fault_status=500, seed=11)
    statuses = [(await t.request("GET", "/x")).status for _ in range(2000)]
    fault_frac = sum(1 for s in statuses if s == 500) / len(statuses)
    assert fault_frac == pytest.approx(0.3, abs=0.05)


async def test_responder_overrides_default() -> None:
    t = FakeTransport(responders={"/auth/login": token_responder("abc123")}, seed=5)
    resp = await t.request("POST", "/auth/login", json={"email": "a@b.c", "password": "x"})
    assert resp.status == 200
    assert resp.body["access_token"] == "abc123"
    # Other paths still hit the default.
    other = await t.request("GET", "/sessions/s")
    assert other.status == 200


async def test_aclose_is_idempotent() -> None:
    t = FakeTransport()
    assert t.closed is False
    await t.aclose()
    await t.aclose()
    assert t.closed is True
