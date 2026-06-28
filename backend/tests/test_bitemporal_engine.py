"""Bitemporal canon engine integration tests (kinora.md §8) — real Postgres.

Covers the DB-backed half of the engine (SKIP when ``KINORA_TEST_DATABASE_URL`` is unset,
mirroring ``test_memory_canon.py``):

* the bitemporal state service — assert / correct / retire / 4-D ``as_of`` / fact history;
* the append-only hash-chained audit log (and tamper detection);
* FORK / DIFF / MERGE over branches, including a concurrent-edit LWW conflict;
* the new MCP tools end-to-end through the single ``MemoryTools.dispatch`` path.

Each test rolls back on teardown — the services only flush.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.db import models  # noqa: F401  (register tables on Base.metadata)
from app.db.base import Base
from app.db.repositories.bitemporal import (
    BitemporalStateRepo,
    CanonAuditRepo,
    CanonBranchRepo,
)
from app.db.repositories.book import BookRepo
from app.mcp import schemas
from app.mcp.tools import MemoryTools
from app.memory.audit_log import AuditLog
from app.memory.branch_service import BranchService
from app.memory.budget_service import BudgetLimits
from app.memory.crdt import HLC, HLCClock, Stamp
from app.memory.temporal_state_service import FactNotFoundError, TemporalStateService

_DB_URL = os.environ.get("KINORA_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not _DB_URL, reason="KINORA_TEST_DATABASE_URL not set; skipping DB integration tests"
)

_DIM = 1152


class FakeEmbedder:
    """A no-op embedder (the bitemporal engine never embeds; here to build MemoryTools)."""

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        return [[0.0] * _DIM for _ in texts]

    async def embed_images(self, images: list[bytes]) -> list[list[float]]:
        return [[0.0] * _DIM for _ in images]


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    assert _DB_URL is not None
    engine = create_async_engine(_DB_URL, poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.run_sync(Base.metadata.create_all)
    db = async_sessionmaker(engine, expire_on_commit=False)()
    try:
        yield db
    finally:
        await db.rollback()
        await db.close()
        await engine.dispose()


def _manual_clock(actor: str, start_ms: int = 1000) -> HLCClock:
    """A deterministic, ever-advancing HLC clock for a test actor."""
    state = {"ms": start_ms}

    def now_ms() -> int:
        state["ms"] += 1
        return state["ms"]

    return HLCClock(actor, now_ms=now_ms)


def _temporal(session: AsyncSession, actor: str = "system") -> TemporalStateService:
    return TemporalStateService(
        BitemporalStateRepo(session),
        AuditLog(CanonAuditRepo(session)),
        actor_id=actor,
        clock_factory=lambda a: _manual_clock(a),
    )


def _branch_service(session: AsyncSession, actor: str = "system") -> BranchService:
    return BranchService(
        BitemporalStateRepo(session),
        CanonBranchRepo(session),
        AuditLog(CanonAuditRepo(session)),
        _temporal(session, actor),
        actor_id=actor,
    )


# --- bitemporal state service ------------------------------------------------ #


async def test_assert_and_as_of_current(session: AsyncSession) -> None:
    book = await (BookRepo(session)).create(title="Bitemporal")
    svc = _temporal(session)
    fact = await svc.assert_fact(
        book_id=book.id,
        subject_entity_key="hero",
        predicate="possesses",
        object_value="sword",
        valid_from_beat=12,
    )
    assert fact.current and fact.valid.valid_from_beat == 12

    # Active at beat 20 (>= 12, still open).
    active = await svc.as_of(book_id=book.id, beat=20)
    assert any(f.fact_key == fact.fact_key for f in active)
    # Not yet active at beat 5 (< 12).
    early = await svc.as_of(book_id=book.id, beat=5)
    assert all(f.fact_key != fact.fact_key for f in early)


async def test_correction_preserves_past_belief(session: AsyncSession) -> None:
    """The headline bitemporal property: 'canon as of any past write'."""
    book = await (BookRepo(session)).create(title="Correction")
    svc = _temporal(session)
    fact = await svc.assert_fact(
        book_id=book.id,
        subject_entity_key="hero",
        predicate="hair",
        object_value="black",
        valid_from_beat=1,
    )
    before_correction = datetime.now(UTC)

    corrected = await svc.correct_fact(
        book_id=book.id, fact_key=fact.fact_key, new_object="silver"
    )
    assert corrected.object_value == "silver"

    # Current belief at beat 10 → silver.
    now = await svc.as_of(book_id=book.id, beat=10)
    assert [f.object_value for f in now if f.fact_key == fact.fact_key] == ["silver"]

    # As-of *before* the correction → the old belief (black) is reconstructable.
    past = await svc.as_of(book_id=book.id, beat=10, as_of_tx=before_correction)
    assert [f.object_value for f in past if f.fact_key == fact.fact_key] == ["black"]

    # Full transaction-time history shows both beliefs.
    history = await svc.history(book_id=book.id, fact_key=fact.fact_key)
    assert [b.object_value for b in history.beliefs] == ["black", "silver"]
    # The superseded belief is tx-closed; the successor is current.
    assert history.beliefs[0].current is False
    assert history.beliefs[1].current is True


async def test_forgetting_scopes_valid_interval(session: AsyncSession) -> None:
    book = await (BookRepo(session)).create(title="Forgetting")
    svc = _temporal(session)
    fact = await svc.assert_fact(
        book_id=book.id,
        subject_entity_key="hero",
        predicate="possesses",
        object_value="sword",
        valid_from_beat=12,
    )
    await svc.retire_fact(book_id=book.id, fact_key=fact.fact_key, valid_to_beat=34)

    # Active inside the interval (beat 20).
    inside = await svc.as_of(book_id=book.id, beat=20)
    assert any(f.fact_key == fact.fact_key for f in inside)
    # Gone after it (beat 35 > 34) — forward generation can't retrieve a stale truth.
    after = await svc.as_of(book_id=book.id, beat=35)
    assert all(f.fact_key != fact.fact_key for f in after)


async def test_correct_unknown_fact_raises(session: AsyncSession) -> None:
    book = await (BookRepo(session)).create(title="Missing")
    svc = _temporal(session)
    with pytest.raises(FactNotFoundError):
        await svc.correct_fact(book_id=book.id, fact_key="nope", new_object="x")


# --- audit log --------------------------------------------------------------- #


async def test_audit_log_chain_is_intact_and_records_every_mutation(
    session: AsyncSession,
) -> None:
    book = await (BookRepo(session)).create(title="Audit")
    svc = _temporal(session, actor="director_42")
    fact = await svc.assert_fact(
        book_id=book.id,
        subject_entity_key="hero",
        predicate="status",
        object_value="alive",
        valid_from_beat=1,
    )
    await svc.correct_fact(book_id=book.id, fact_key=fact.fact_key, new_object="wounded")
    await svc.retire_fact(book_id=book.id, fact_key=fact.fact_key, valid_to_beat=50)

    chain = await AuditLog(CanonAuditRepo(session)).replay(book.id)
    assert chain.intact and chain.broken_at_seq is None
    assert [e.action for e in chain.entries] == [
        "assert_fact",
        "correct_fact",
        "retire_fact",
    ]
    assert all(e.actor_id == "director_42" for e in chain.entries)
    # The chain links: each entry's prev_hash is the previous entry's hash.
    assert chain.entries[0].prev_hash is None
    assert chain.entries[1].prev_hash == chain.entries[0].entry_hash
    assert chain.entries[2].prev_hash == chain.entries[1].entry_hash


async def test_audit_log_detects_tampering(session: AsyncSession) -> None:
    book = await (BookRepo(session)).create(title="Tamper")
    svc = _temporal(session)
    f = await svc.assert_fact(
        book_id=book.id,
        subject_entity_key="x",
        predicate="p",
        object_value="v1",
        valid_from_beat=1,
    )
    await svc.correct_fact(book_id=book.id, fact_key=f.fact_key, new_object="v2")

    # Retroactively rewrite a payload directly in the DB (a forge).
    await session.execute(
        text(
            "UPDATE canon_audit SET payload = '{\"object\": \"FORGED\"}'::jsonb "
            "WHERE book_id = :b AND seq = 1"
        ).bindparams(b=book.id)
    )
    await session.flush()

    chain = await AuditLog(CanonAuditRepo(session)).replay(book.id)
    assert chain.intact is False
    assert chain.broken_at_seq == 1  # the forged row fails re-hash


# --- fork / diff / merge ----------------------------------------------------- #


async def test_fork_seeds_branch_and_diff_shows_edits(session: AsyncSession) -> None:
    book = await (BookRepo(session)).create(title="Fork")
    main = _temporal(session)
    await main.assert_fact(
        book_id=book.id,
        subject_entity_key="hero",
        predicate="hair",
        object_value="black",
        valid_from_beat=1,
        fact_key="fk_hair",
    )
    branches = _branch_service(session)
    await branches.fork(book_id=book.id, name="director_cut")

    # Edit the fork only.
    fork_svc = _temporal(session)
    await fork_svc.correct_fact(
        book_id=book.id, fact_key="fk_hair", new_object="silver", branch="director_cut"
    )
    # Add a new fact on the fork.
    await fork_svc.assert_fact(
        book_id=book.id,
        subject_entity_key="hero",
        predicate="wields",
        object_value="staff",
        valid_from_beat=2,
        branch="director_cut",
        fact_key="fk_staff",
    )

    diff = await branches.diff(book_id=book.id, branch_a="main", branch_b="director_cut")
    changes = {c.fact_key: c for c in diff.changes}
    assert changes["fk_hair"].change == "changed"
    assert changes["fk_hair"].object_before == "black"
    assert changes["fk_hair"].object_after == "silver"
    assert changes["fk_staff"].change == "added"


async def test_merge_fast_forward_applies_source_edits(session: AsyncSession) -> None:
    book = await (BookRepo(session)).create(title="Merge FF")
    main = _temporal(session)
    await main.assert_fact(
        book_id=book.id,
        subject_entity_key="hero",
        predicate="hair",
        object_value="black",
        valid_from_beat=1,
        fact_key="fk_hair",
    )
    branches = _branch_service(session)
    await branches.fork(book_id=book.id, name="edit")
    fork_svc = _temporal(session)
    await fork_svc.correct_fact(
        book_id=book.id, fact_key="fk_hair", new_object="silver", branch="edit"
    )

    result = await branches.merge(book_id=book.id, source="edit", target="main")
    assert result.applied == 1
    assert result.strategy in {"fast_forward", "merged"}

    # main now reflects the edit.
    on_main = await main.as_of(book_id=book.id, beat=10, branch="main")
    assert [f.object_value for f in on_main if f.fact_key == "fk_hair"] == ["silver"]


async def test_merge_concurrent_edit_resolves_by_lww(session: AsyncSession) -> None:
    """Both branches edit the same fact concurrently → higher CRDT stamp wins; loss reported."""
    book = await (BookRepo(session)).create(title="Merge conflict")
    states = BitemporalStateRepo(session)
    # Seed main with a fact carrying a *low* stamp.
    await states.insert(
        book_id=book.id,
        fact_key="fk_coat",
        branch="main",
        subject_entity_key="hero",
        predicate="coat",
        object_value="blue",
        valid_from_beat=1,
        valid_to_beat=None,
        tx_from=datetime.now(UTC),
        stamp_wall=100,
        stamp_counter=0,
        actor_id="ingest",
    )
    # A fork branch holds a competing belief with a *higher* stamp (later edit).
    await states.insert(
        book_id=book.id,
        fact_key="fk_coat",
        branch="edit",
        subject_entity_key="hero",
        predicate="coat",
        object_value="red",
        valid_from_beat=1,
        valid_to_beat=None,
        tx_from=datetime.now(UTC),
        stamp_wall=200,
        stamp_counter=0,
        actor_id="director",
    )
    await CanonBranchRepo(session).create(
        book_id=book.id, name="edit", parent="main", base_beat=None, base_tx=None
    )

    branches = _branch_service(session)
    result = await branches.merge(book_id=book.id, source="edit", target="main")
    assert result.strategy == "merged"
    assert len(result.conflicts) == 1
    conflict = result.conflicts[0]
    assert conflict.winner == "source"  # red's stamp (200) dominates blue's (100)
    # main now holds the winning belief.
    on_main = await _temporal(session).as_of(book_id=book.id, beat=10, branch="main")
    assert [f.object_value for f in on_main if f.fact_key == "fk_coat"] == ["red"]


# --- LWW dedup in as_of ------------------------------------------------------ #


async def test_as_of_dedups_to_highest_stamp(session: AsyncSession) -> None:
    book = await (BookRepo(session)).create(title="Dedup")
    states = BitemporalStateRepo(session)
    now = datetime.now(UTC)
    for wall, value in ((100, "old"), (300, "new"), (200, "mid")):
        await states.insert(
            book_id=book.id,
            fact_key="fk",
            branch="main",
            subject_entity_key="s",
            predicate="p",
            object_value=value,
            valid_from_beat=1,
            valid_to_beat=None,
            tx_from=now,
            stamp_wall=wall,
            stamp_counter=0,
            actor_id="a",
        )
    rows = await states.as_of(book.id, "main", 5)
    # All three are "current" beliefs of the same fact_key; the highest stamp wins.
    assert len(rows) == 1 and rows[0].object_value == "new"


# --- temporal compaction ----------------------------------------------------- #


async def test_compaction_prunes_old_superseded_keeps_current_and_audit(
    session: AsyncSession,
) -> None:
    from datetime import timedelta

    from app.db.models.bitemporal import AuditAction
    from app.memory.audit_log import AuditLog as _AuditLog
    from app.memory.compaction import TemporalCompactor

    book = await (BookRepo(session)).create(title="Compact")
    states = BitemporalStateRepo(session)
    base = datetime.now(UTC) - timedelta(days=365)
    # Three superseded beliefs (closed long ago) + one current belief.
    for i, (frm, to, val) in enumerate(
        [
            (base, base + timedelta(days=1), "v1"),
            (base + timedelta(days=1), base + timedelta(days=2), "v2"),
            (base + timedelta(days=2), base + timedelta(days=3), "v3"),
        ]
    ):
        row = await states.insert(
            book_id=book.id,
            fact_key="fk",
            branch="main",
            subject_entity_key="s",
            predicate="p",
            object_value=val,
            valid_from_beat=1,
            valid_to_beat=None,
            tx_from=frm,
            stamp_wall=100 + i,
            stamp_counter=0,
            actor_id="a",
        )
        await states.close_tx(row.id, to)
    await states.insert(
        book_id=book.id,
        fact_key="fk",
        branch="main",
        subject_entity_key="s",
        predicate="p",
        object_value="current",
        valid_from_beat=1,
        valid_to_beat=None,
        tx_from=base + timedelta(days=3),
        stamp_wall=999,
        stamp_counter=0,
        actor_id="a",
    )
    # Record an audit row so we can prove compaction does not touch the chain.
    audit = _AuditLog(CanonAuditRepo(session))
    await audit.record(
        book_id=book.id,
        branch="main",
        action=AuditAction.ASSERT_FACT,
        actor_id="a",
        target_key="fk",
        payload={"object": "v1"},
    )

    compactor = TemporalCompactor(states)
    plan = await compactor.plan(book_id=book.id, branch="main", horizon_days=30)
    # 3 superseded; keep the newest superseded + the current → 2 prunable (v1, v2).
    assert plan.prune_count == 2
    assert plan.kept_current == 1

    result = await compactor.compact(book_id=book.id, branch="main", horizon_days=30)
    assert result.pruned == 2

    # Current belief still resolves after compaction.
    rows = await states.as_of(book.id, "main", 5)
    assert [r.object_value for r in rows] == ["current"]
    # Audit chain is untouched and still verifies.
    chain = await audit.replay(book.id)
    assert chain.intact


async def test_compaction_keeps_recent_superseded(session: AsyncSession) -> None:
    from app.memory.compaction import TemporalCompactor

    book = await (BookRepo(session)).create(title="Recent")
    svc = _temporal(session)
    f = await svc.assert_fact(
        book_id=book.id,
        subject_entity_key="s",
        predicate="p",
        object_value="a",
        valid_from_beat=1,
    )
    await svc.correct_fact(book_id=book.id, fact_key=f.fact_key, new_object="b")
    # The supersession just happened (well inside the horizon) → nothing prunable.
    plan = await TemporalCompactor(BitemporalStateRepo(session)).plan(
        book_id=book.id, branch="main", horizon_days=30
    )
    assert plan.prune_count == 0


# --- MCP tools through dispatch (the single execution path) ------------------ #


def _memory_tools(session: AsyncSession) -> MemoryTools:
    @asynccontextmanager
    async def session_factory() -> AsyncIterator[AsyncSession]:
        # Reuse the single test session so writes are visible within the rolled-back tx.
        yield session

    return MemoryTools(
        embedder=FakeEmbedder(),
        session_factory=session_factory,
        limits=BudgetLimits(
            ceiling_video_s=100.0,
            per_session_s=50.0,
            per_scene_s=25.0,
            low_floor_s=10.0,
            live_video=False,
        ),
    )


async def test_bitemporal_tools_through_dispatch(session: AsyncSession) -> None:
    book = await (BookRepo(session)).create(title="Dispatch")
    tools = _memory_tools(session)

    # assert via dispatch
    asserted = await tools.dispatch(
        "canon.assert_fact",
        {
            "book_id": book.id,
            "subject_entity_key": "hero",
            "predicate": "status",
            "object_value": "alive",
            "valid_from_beat": 1,
            "actor_id": "tester",
        },
    )
    fact_key = asserted.fact_key  # type: ignore[attr-defined]

    # correct via dispatch
    await tools.dispatch(
        "canon.correct_fact",
        {"book_id": book.id, "fact_key": fact_key, "new_object": "captured"},
    )

    # facts_as_of via dispatch → current belief is the correction
    out = await tools.dispatch("canon.facts_as_of", {"book_id": book.id, "beat": 10})
    objs = [f.object_value for f in out.facts]  # type: ignore[attr-defined]
    assert objs == ["captured"]

    # fork + diff + merge via dispatch
    await tools.dispatch(
        "canon.fork", {"book_id": book.id, "name": "cut", "actor_id": "tester"}
    )
    await tools.dispatch(
        "canon.correct_fact",
        {"book_id": book.id, "fact_key": fact_key, "new_object": "freed", "branch": "cut"},
    )
    diff = await tools.dispatch(
        "canon.diff", {"book_id": book.id, "branch_a": "main", "branch_b": "cut"}
    )
    assert any(c.change == "changed" for c in diff.changes)  # type: ignore[attr-defined]

    merge = await tools.dispatch(
        "canon.merge", {"book_id": book.id, "source": "cut", "target": "main"}
    )
    assert merge.applied >= 1  # type: ignore[attr-defined]

    # audit via dispatch → chain intact, mutations recorded
    chain = await tools.dispatch("canon.audit", {"book_id": book.id})
    assert chain.intact  # type: ignore[attr-defined]
    actions = {e.action for e in chain.entries}  # type: ignore[attr-defined]
    assert {"assert_fact", "correct_fact", "fork_branch", "merge_branch"} <= actions

    # view via dispatch → the inspectable read contract
    view = await tools.dispatch("canon.view", {"book_id": book.id})
    assert view.book_id == book.id  # type: ignore[attr-defined]
    assert any(b.name == "cut" for b in view.branches)  # type: ignore[attr-defined]
    assert len(view.audit_tail) > 0  # type: ignore[attr-defined]

    # vault via dispatch → inspectable markdown with all sections
    vault = await tools.dispatch("canon.vault", {"book_id": book.id})
    assert "Active canon facts" in vault.markdown  # type: ignore[attr-defined]
    assert "Audit log" in vault.markdown  # type: ignore[attr-defined]

    # compact (dry-run) via dispatch → nothing prunable (edits are recent)
    plan = await tools.dispatch("canon.compact", {"book_id": book.id})
    assert plan.dry_run is True and plan.pruned == 0  # type: ignore[attr-defined]


async def test_dispatch_validates_input_model() -> None:
    # The single dispatch path validates args into the tool's pydantic model.
    inp = schemas.CanonAssertFactInput.model_validate(
        {
            "book_id": "b1",
            "subject_entity_key": "s",
            "predicate": "p",
            "object_value": "o",
            "valid_from_beat": 3,
        }
    )
    assert inp.branch == "main" and inp.actor_id == "system"


def test_crdt_stamp_ordering_used_by_merge() -> None:
    # Sanity: the Stamp total order the merge relies on.
    low = Stamp(HLC(100, 0), "a")
    high = Stamp(HLC(200, 0), "a")
    assert high > low and high.dominates(low)
