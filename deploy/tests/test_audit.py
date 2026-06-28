"""Tests for the append-only audit trail (offline, virtual clock)."""

from __future__ import annotations

from deploy.orchestrator.audit import AuditTrail, InMemoryAuditSink
from deploy.orchestrator.fakes import VirtualClock
from deploy.orchestrator.models import DeployEvent, DeployState


def test_sequence_is_monotonic_and_stamped() -> None:
    clock = VirtualClock()
    trail = AuditTrail("d1", now=clock)
    e1 = trail.record(DeployState.PENDING, "plan", "planning")
    clock.advance(2.0)
    e2 = trail.record(DeployState.HYDRATING, "hydrate", "hydrating")
    assert e1.seq == 1 and e2.seq == 2
    assert e1.at == 0.0 and e2.at == 2.0
    assert e1.deploy_id == "d1"


def test_detail_is_copied_not_referenced() -> None:
    trail = AuditTrail("d1", now=VirtualClock())
    detail = {"slot": "s1"}
    event = trail.record(DeployState.PROVISIONING, "provision", "up", **detail)
    detail["slot"] = "mutated"
    assert event.detail["slot"] == "s1"


def test_by_kind_and_last() -> None:
    trail = AuditTrail("d1", now=VirtualClock())
    trail.record(DeployState.ROLLING_OUT, "step", "a")
    trail.record(DeployState.ROLLING_OUT, "traffic", "shift")
    trail.record(DeployState.ROLLING_OUT, "step", "b")
    steps = trail.by_kind("step")
    assert [e.message for e in steps] == ["a", "b"]
    last = trail.last()
    assert last is not None and last.kind == "step"


def test_in_memory_sink_iteration() -> None:
    sink = InMemoryAuditSink()
    trail = AuditTrail("d1", sink=sink, now=VirtualClock())
    trail.record(DeployState.PENDING, "plan", "x")
    assert len(sink) == 1
    assert list(sink)[0].kind == "plan"


def test_render_transcript_contains_states() -> None:
    trail = AuditTrail("d1", now=VirtualClock())
    trail.record(DeployState.PENDING, "plan", "planning", n=3)
    text = trail.render()
    assert "pending" in text
    assert "planning" in text
    assert "n=3" in text


def test_custom_sink_is_used() -> None:
    captured: list[str] = []

    class CapturingSink:
        def append(self, event: DeployEvent) -> None:
            captured.append(event.kind)

        def events(self) -> list[DeployEvent]:
            return []

    trail = AuditTrail("d1", sink=CapturingSink(), now=VirtualClock())
    trail.record(DeployState.PENDING, "plan", "x")
    assert captured == ["plan"]
