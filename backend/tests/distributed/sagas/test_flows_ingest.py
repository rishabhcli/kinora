"""The ingest → canon-build → identity-lock saga, end to end over fakes."""

from __future__ import annotations

from app.distributed.sagas.backoff import BackoffPolicy
from app.distributed.sagas.definition import SagaRegistry
from app.distributed.sagas.effects import InMemoryEffectLedger
from app.distributed.sagas.flows.fakes import FakeIngestServices
from app.distributed.sagas.flows.ingest import build_ingest_saga, initial_ingest_state
from app.distributed.sagas.orchestrator import SagaOrchestrator
from app.distributed.sagas.store import InMemorySagaStore
from app.distributed.sagas.types import SagaStatus
from app.jobs.clock import ManualClock


def _build(fake: FakeIngestServices, *, retry: BackoffPolicy | None = None) -> tuple[
    SagaOrchestrator, InMemorySagaStore
]:
    clock = ManualClock()
    store = InMemorySagaStore()
    reg = SagaRegistry()
    reg.register(build_ingest_saga(retry=retry or BackoffPolicy(max_attempts=2, base_delay_s=0.0)))
    orch = SagaOrchestrator(
        store,
        reg,
        clock=clock,
        effects=InMemoryEffectLedger(clock=clock),
        resources={"ingest_ports": fake},
    )
    return orch, store


async def test_happy_path_marks_book_ready() -> None:
    fake = FakeIngestServices()
    orch, _ = _build(fake)
    inst = await orch.run_to_completion(
        "ingest_canon_identity", "book-1", initial_state=initial_ingest_state("book-1", "oss://up/1")
    )
    assert inst.status is SagaStatus.COMPLETED
    assert "book-1" in fake.ready
    assert fake.canon["book-1"] == 1
    assert len(fake.locked["book-1"]) == 2
    assert inst.state["object_key"] == "oss://staged/book-1.pdf"
    # No compensation ran.
    assert "delete_canon" not in fake.calls
    assert "mark_failed" not in fake.calls


async def test_canon_failure_rolls_back_earlier_steps() -> None:
    """build_canon fails permanently → stage + extract are compensated in reverse."""
    fake = FakeIngestServices(fail={"build_canon": 10_000})  # always fail
    orch, _ = _build(fake)
    inst = await orch.run_to_completion(
        "ingest_canon_identity", "book-2", initial_state=initial_ingest_state("book-2", "oss://up/2")
    )
    assert inst.status is SagaStatus.COMPENSATED
    # Forward got through stage + extract; canon never produced.
    assert "book-2" not in fake.canon
    # Compensations ran in reverse: pages dropped, source deleted.
    assert "drop_pages" in fake.calls
    assert "delete_source" in fake.calls
    assert "oss://staged/book-2.pdf" not in fake.staged
    # The book is never marked ready.
    assert "book-2" not in fake.ready


async def test_transient_failure_retries_then_succeeds() -> None:
    """A transient build_canon fault is retried (within budget) and the saga commits."""
    fake = FakeIngestServices(fail={"build_canon": 1})  # fail once, then succeed
    orch, _ = _build(fake, retry=BackoffPolicy(max_attempts=3, base_delay_s=0.0))
    inst = await orch.run_to_completion(
        "ingest_canon_identity", "book-3", initial_state=initial_ingest_state("book-3", "oss://up/3")
    )
    assert inst.status is SagaStatus.COMPLETED
    assert fake.canon["book-3"] == 1
    assert fake.calls.count("build_canon") == 2  # one fail + one success


async def test_empty_pdf_is_terminal_and_rolls_back() -> None:
    """An empty extraction is a non-retryable failure → immediate rollback of stage."""
    fake = FakeIngestServices(page_count=0)
    orch, _ = _build(fake)
    inst = await orch.run_to_completion(
        "ingest_canon_identity", "book-4", initial_state=initial_ingest_state("book-4", "oss://up/4")
    )
    assert inst.status is SagaStatus.COMPENSATED
    # extract_pages itself returned 0 → its own forward is undone (drop_pages) + stage.
    assert "delete_source" in fake.calls
    assert "build_canon" not in fake.calls


async def test_mark_ready_failure_rolls_back_full_chain() -> None:
    """A failure at the LAST step compensates every prior step (full reverse undo)."""
    fake = FakeIngestServices(fail={"mark_ready": 10_000})
    orch, _ = _build(fake)
    inst = await orch.run_to_completion(
        "ingest_canon_identity", "book-5", initial_state=initial_ingest_state("book-5", "oss://up/5")
    )
    assert inst.status is SagaStatus.COMPENSATED
    # Everything undone: identity unlocked, canon deleted, pages dropped, source gone.
    assert "unlock_identity" in fake.calls
    assert "delete_canon" in fake.calls
    assert "drop_pages" in fake.calls
    assert "delete_source" in fake.calls
    assert "book-5" not in fake.canon
    assert "book-5" not in fake.locked


async def test_idempotent_effects_no_double_stage_on_retry() -> None:
    """A retried later step never re-runs an earlier step's side effect."""
    # Fail lock_identity once so the saga retries it; stage/extract/canon must not
    # re-run their effects (they already completed and were persisted).
    fake = FakeIngestServices(fail={"lock_identity": 1})
    orch, _ = _build(fake, retry=BackoffPolicy(max_attempts=3, base_delay_s=0.0))
    inst = await orch.run_to_completion(
        "ingest_canon_identity", "book-6", initial_state=initial_ingest_state("book-6", "oss://up/6")
    )
    assert inst.status is SagaStatus.COMPLETED
    assert fake.calls.count("stage_source") == 1
    assert fake.calls.count("extract_pages") == 1
    assert fake.calls.count("build_canon") == 1
    assert fake.calls.count("lock_identity") == 2  # the retried step
