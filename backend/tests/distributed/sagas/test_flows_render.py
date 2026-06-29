"""The render → QA → conflict → degrade saga (§9.7 + §7.2 + §12.4) over fakes."""

from __future__ import annotations

from app.distributed.sagas.backoff import BackoffPolicy
from app.distributed.sagas.definition import SagaRegistry
from app.distributed.sagas.effects import InMemoryEffectLedger
from app.distributed.sagas.flows.fakes import FakeRenderServices
from app.distributed.sagas.flows.render import (
    ArbitrationDecision,
    Conflict,
    arbitrate,
    build_render_saga,
    initial_render_state,
)
from app.distributed.sagas.orchestrator import SagaOrchestrator
from app.distributed.sagas.store import InMemorySagaStore
from app.distributed.sagas.types import SagaStatus
from app.jobs.clock import ManualClock


def _build(fake: FakeRenderServices) -> tuple[SagaOrchestrator, InMemorySagaStore]:
    clock = ManualClock()
    store = InMemorySagaStore()
    reg = SagaRegistry()
    # Zero-delay retries so QA regenerations resolve within one drive.
    reg.register(
        build_render_saga(
            qa_retry=BackoffPolicy(max_attempts=3, base_delay_s=0.0),
            render_retry=BackoffPolicy(max_attempts=3, base_delay_s=0.0),
        )
    )
    orch = SagaOrchestrator(
        store,
        reg,
        clock=clock,
        effects=InMemoryEffectLedger(clock=clock),
        resources={"render_ports": fake},
    )
    return orch, store


def _conflict(*, textual: bool, user_facing: bool = True) -> Conflict:
    return Conflict(
        conflict_id="cf_001",
        shot_id="shot_00051",
        claim="heroine draws a sword",
        canon_fact="sword retired at beat_0034",
        has_textual_support=textual,
        user_facing=user_facing,
    )


# --------------------------------------------------------------------------- #
# §7.2 arbitration policy (pure function)
# --------------------------------------------------------------------------- #
def test_arbitrate_evolves_with_textual_support() -> None:
    assert arbitrate(_conflict(textual=True), director_present=False) is (
        ArbitrationDecision.EVOLVE_CANON
    )


def test_arbitrate_surfaces_to_director_when_present() -> None:
    assert arbitrate(_conflict(textual=False), director_present=True) is (
        ArbitrationDecision.SURFACE_TO_USER
    )


def test_arbitrate_honors_canon_as_safe_default() -> None:
    assert arbitrate(_conflict(textual=False), director_present=False) is (
        ArbitrationDecision.HONOR_CANON
    )
    # Not user-facing → honor even with a director present.
    assert arbitrate(
        _conflict(textual=False, user_facing=False), director_present=True
    ) is ArbitrationDecision.HONOR_CANON


# --------------------------------------------------------------------------- #
# End-to-end render saga
# --------------------------------------------------------------------------- #
async def test_clean_render_accepts() -> None:
    fake = FakeRenderServices()  # QA always passes
    orch, _ = _build(fake)
    inst = await orch.run_to_completion(
        "render_qa_conflict_degrade", "shot-1", initial_state=initial_render_state("shot-1", "h1")
    )
    assert inst.status is SagaStatus.COMPLETED
    assert inst.state["accepted"] is True
    assert len(fake.rendered) == 1
    assert len(fake.accepted) == 1
    assert len(fake.reservations) == 1  # reserved, not released


async def test_cache_hit_short_circuits_zero_budget() -> None:
    fake = FakeRenderServices(cache={"h2": "cached_clip_7"})
    orch, _ = _build(fake)
    inst = await orch.run_to_completion(
        "render_qa_conflict_degrade", "shot-2", initial_state=initial_render_state("shot-2", "h2")
    )
    assert inst.status is SagaStatus.COMPLETED
    assert inst.state["clip_id"] == "cached_clip_7"
    # No budget reserved, no render, no QA call (it short-circuits as cached).
    assert "reserve_budget" not in fake.calls
    assert "render_clip" not in fake.calls
    assert inst.state.get("reservation_id") is None


async def test_honor_canon_regenerates_then_accepts() -> None:
    """A conflict without textual support → honor_canon → regenerate, then pass."""
    fake = FakeRenderServices(
        conflict=_conflict(textual=False, user_facing=False), conflict_times=1
    )
    orch, _ = _build(fake)
    inst = await orch.run_to_completion(
        "render_qa_conflict_degrade", "shot-3", initial_state=initial_render_state("shot-3", "h3")
    )
    assert inst.status is SagaStatus.COMPLETED
    # One regeneration: two render calls, two QA calls, accepted.
    assert fake.calls.count("render_clip") == 2
    assert fake.calls.count("qa_clip") == 2
    assert inst.state["accepted"] is True
    assert fake.evolved == []  # honor_canon does NOT evolve


async def test_evolve_canon_updates_then_regenerates() -> None:
    """A conflict WITH textual support → evolve_canon (records the evolution) → regen."""
    fake = FakeRenderServices(conflict=_conflict(textual=True), conflict_times=1)
    orch, _ = _build(fake)
    inst = await orch.run_to_completion(
        "render_qa_conflict_degrade", "shot-4", initial_state=initial_render_state("shot-4", "h4")
    )
    assert inst.status is SagaStatus.COMPLETED
    assert fake.evolved == ["cf_001"]  # canon was evolved exactly once
    assert inst.state["accepted"] is True


async def test_persistent_conflict_degrades_to_ken_burns() -> None:
    """A conflict that never clears exhausts QA retries → degrade ladder, still accepts."""
    fake = FakeRenderServices(
        conflict=_conflict(textual=False, user_facing=False), conflict_times=10_000
    )
    orch, _ = _build(fake)
    inst = await orch.run_to_completion(
        "render_qa_conflict_degrade", "shot-5", initial_state=initial_render_state("shot-5", "h5")
    )
    # The film never hard-stops: it degrades and accepts a Ken-Burns clip.
    assert inst.status is SagaStatus.COMPLETED
    assert inst.state["degraded"] is True
    assert inst.state["qa"] == "degraded"
    # The accepted clip is a Ken-Burns one.
    assert any(c.endswith("_kb") for c in fake.accepted)


async def test_surface_to_user_parks_without_accepting() -> None:
    """A user-facing conflict with a director present surfaces for a pick (parks)."""
    fake = FakeRenderServices(
        conflict=_conflict(textual=False, user_facing=True),
        conflict_times=10_000,
        director_present=True,
    )
    orch, _ = _build(fake)
    inst = await orch.run_to_completion(
        "render_qa_conflict_degrade", "shot-6", initial_state=initial_render_state("shot-6", "h6")
    )
    # Saga completes structurally but the shot is parked awaiting the director.
    assert inst.status is SagaStatus.COMPLETED
    assert inst.state["qa"] == "awaiting_user"
    assert inst.state["conflict_id"] == "cf_001"
    assert inst.state.get("accepted") is False
    assert len(fake.accepted) == 0


async def test_render_provider_failure_releases_budget() -> None:
    """A render that fails past its retry budget rolls back, releasing the reservation."""
    fake = FakeRenderServices(fail={"render_clip": 10_000})
    orch, _ = _build(fake)
    inst = await orch.run_to_completion(
        "render_qa_conflict_degrade", "shot-7", initial_state=initial_render_state("shot-7", "h7")
    )
    assert inst.status is SagaStatus.COMPENSATED
    # The budget reservation was released (no double-spend).
    assert len(fake.released) == 1
    assert len(fake.reservations) == 0
    assert len(fake.accepted) == 0
