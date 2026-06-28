"""Unit tests for synthetic-monitoring canaries (app.reliability.canary)."""

from __future__ import annotations

from app.reliability.canary import (
    CanaryRunner,
    Journey,
    JourneyStep,
    kinora_read_journey,
)
from app.reliability.runner import VirtualClock
from app.reliability.transport import FakeTransport, Response, token_responder


def _session_responder(session_id: str = "sess_canary_1"):  # type: ignore[no-untyped-def]
    def _respond(method: str, path: str, json: object) -> Response | None:
        return Response(status=201, elapsed_ms=0.0, body={"session_id": session_id})

    return _respond


def _healthy_transport(*, latency_ms: float = 10.0) -> FakeTransport:
    return FakeTransport(
        base_latency_ms=latency_ms,
        latency_jitter_ms=0.0,
        seed=1,
        responders={
            "/auth/login": token_responder("tok-123"),
            "/sessions": _session_responder(),
        },
    )


async def test_healthy_journey_passes_all_steps() -> None:
    transport = _healthy_transport()
    runner = CanaryRunner(transport, clock=VirtualClock().now)
    result = await runner.run(kinora_read_journey())
    assert result.passed is True
    assert [s.name for s in result.steps] == [
        "login",
        "library",
        "open_session",
        "read_intent",
        "seek",
    ]
    assert result.failures == []
    assert result.total_latency_ms > 0.0


async def test_session_id_threads_into_later_steps() -> None:
    transport = _healthy_transport()
    runner = CanaryRunner(transport, clock=VirtualClock().now)
    await runner.run(kinora_read_journey())
    # The intent + seek steps used the captured session id, not the placeholder.
    intent_call = next(c for c in transport.calls if c.path.endswith("/intent"))
    seek_call = next(c for c in transport.calls if c.path.endswith("/seek"))
    assert "sess_canary_1" in intent_call.path
    assert "sess_canary_1" in seek_call.path


async def test_login_failure_stops_journey() -> None:
    transport = FakeTransport(
        seed=1,
        responders={"/auth/login": token_responder("x", status=401)},
    )
    runner = CanaryRunner(transport, clock=VirtualClock().now)
    result = await runner.run(kinora_read_journey())
    assert result.passed is False
    # Stops at login: only the login step ran.
    assert len(result.steps) == 1
    assert result.steps[0].name == "login"
    assert not result.steps[0].passed


async def test_missing_access_token_fails_body_check() -> None:
    # 200 with no token => body check fails even though status is fine.
    def _bad_login(method: str, path: str, json: object) -> Response | None:
        return Response(status=200, elapsed_ms=5.0, body={"wrong": "shape"})

    transport = FakeTransport(seed=1, responders={"/auth/login": _bad_login})
    runner = CanaryRunner(transport, clock=VirtualClock().now)
    result = await runner.run(kinora_read_journey())
    assert result.passed is False
    assert any("access_token" in r for r in result.steps[0].reasons)


async def test_sla_violation_is_reported() -> None:
    # Intent SLA is 250ms; make the transport slow so intent breaches it.
    transport = FakeTransport(
        base_latency_ms=500.0,
        latency_jitter_ms=0.0,
        seed=1,
        responders={
            "/auth/login": token_responder("tok"),
            "/sessions": _session_responder(),
        },
    )
    runner = CanaryRunner(transport, clock=VirtualClock().now, stop_on_failure=False)
    result = await runner.run(kinora_read_journey())
    assert result.passed is False
    # The fast-SLA steps (intent 250ms, seek 150ms) breach at 500ms latency.
    breached = {s.name for s in result.failures}
    assert "read_intent" in breached
    assert "seek" in breached
    for s in result.failures:
        assert any("SLA" in r for r in s.reasons)


async def test_stop_on_failure_false_runs_all_steps() -> None:
    transport = FakeTransport(
        base_latency_ms=5.0,
        latency_jitter_ms=0.0,
        seed=1,
        fault_rate=1.0,
        fault_status=503,
    )
    runner = CanaryRunner(transport, clock=VirtualClock().now, stop_on_failure=False)
    result = await runner.run(kinora_read_journey())
    assert result.passed is False
    # All five steps attempted despite every one failing.
    assert len(result.steps) == 5


async def test_result_to_dict_and_render() -> None:
    transport = _healthy_transport()
    runner = CanaryRunner(transport, clock=VirtualClock().now)
    result = await runner.run(kinora_read_journey())
    doc = result.to_dict()
    assert doc["journey"] == "kinora_read"
    assert doc["passed"] is True
    steps = doc["steps"]
    assert isinstance(steps, list)
    assert len(steps) == 5
    text = result.render_text()
    assert "Canary 'kinora_read': PASS" in text


async def test_custom_journey() -> None:
    journey = Journey(
        name="ping",
        steps=(
            JourneyStep(
                name="health",
                build=lambda ctx: ("GET", "/health", None),
                sla_ms=100.0,
            ),
        ),
    )
    transport = FakeTransport(base_latency_ms=5.0, latency_jitter_ms=0.0, seed=1)
    runner = CanaryRunner(transport, clock=VirtualClock().now)
    result = await runner.run(journey)
    assert result.passed is True
    assert result.steps[0].name == "health"
