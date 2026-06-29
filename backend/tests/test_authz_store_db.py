"""DB-backed tuple-store + decision-log integration tests (isolated DB, gated).

These exercise :class:`~app.platform.authz.store_db.DbTupleStore` and
:class:`~app.platform.authz.store_db.DbDecisionLog` against a real Postgres,
asserting the DB-backed store produces the *same* graph decisions as the
in-memory one (the whole point of the protocol). They run only when the isolated
authz DB URL is configured; otherwise they skip cleanly so the unit suite still
runs anywhere.

Point ``KINORA_AUTHZ_TEST_DATABASE_URL`` at the isolated DB, e.g.
``postgresql+asyncpg://kinora:kinora@localhost:5433/authzplane_test``.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.platform.authz.db_models import AuthzDecisionLogRow, AuthzRelationTuple
from app.platform.authz.factory import build_relation_graph
from app.platform.authz.rebac import ObjectRef, RelationTuple, SubjectRef
from app.platform.authz.store_db import DbDecisionLog, DbTupleStore

_URL = os.environ.get("KINORA_AUTHZ_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not _URL,
    reason="authz DB tests require KINORA_AUTHZ_TEST_DATABASE_URL (isolated :5433 DB)",
)


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    assert _URL is not None
    engine = create_async_engine(_URL, poolclass=NullPool)
    # Create only this subsystem's tables (additive; never touches others).
    async with engine.begin() as conn:
        await conn.run_sync(AuthzRelationTuple.__table__.create, checkfirst=True)
        await conn.run_sync(AuthzDecisionLogRow.__table__.create, checkfirst=True)
        await conn.execute(AuthzRelationTuple.__table__.delete())
        await conn.execute(AuthzDecisionLogRow.__table__.delete())
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


@pytest.mark.asyncio
async def test_db_tuple_store_roundtrip_and_idempotent(session: AsyncSession) -> None:
    store = await DbTupleStore.load(session)
    t = RelationTuple.of("book:1", "owner", "user:alice")
    await store.awrite(t)
    await store.awrite(t)  # idempotent — no duplicate row
    await session.commit()

    reloaded = await DbTupleStore.load(session)
    graph = build_relation_graph(reloaded)
    assert graph.check(ObjectRef.parse("book:1"), "owner", SubjectRef.user("alice"))
    assert not graph.check(ObjectRef.parse("book:1"), "owner", SubjectRef.user("bob"))

    rows = len(reloaded)
    assert rows == 1  # the duplicate write did not create a second row


@pytest.mark.asyncio
async def test_db_store_matches_in_memory_decisions(session: AsyncSession) -> None:
    # Build a world with parent inheritance, persist it, and assert the DB-backed
    # graph agrees with an in-memory graph holding the same tuples.
    from app.platform.authz.rebac import InMemoryTupleStore

    world = [
        ("workspace:w", "editor", "user:bob"),
        ("book:b", "parent", "workspace:w"),
        ("book:own", "owner", "user:alice"),
    ]
    db = await DbTupleStore.load(session)
    mem = InMemoryTupleStore()
    for o, r, s in world:
        t = RelationTuple.of(o, r, s)
        await db.awrite(t)
        mem.write(t)
    await session.commit()

    db_graph = build_relation_graph(await DbTupleStore.load(session))
    mem_graph = build_relation_graph(mem)

    cases = [
        ("book:b", "editor", "user:bob"),
        ("book:b", "viewer", "user:bob"),
        ("book:b", "owner", "user:bob"),
        ("book:own", "owner", "user:alice"),
        ("book:b", "viewer", "user:stranger"),
    ]
    for obj, rel, subj in cases:
        assert db_graph.check(
            ObjectRef.parse(obj), rel, SubjectRef.parse(subj)
        ) == mem_graph.check(ObjectRef.parse(obj), rel, SubjectRef.parse(subj)), (
            obj, rel, subj,
        )

    # reverse index also agrees
    assert {o.id for o in db_graph.list_objects("book", "viewer", SubjectRef.user("bob"))} == {
        o.id for o in mem_graph.list_objects("book", "viewer", SubjectRef.user("bob"))
    }


@pytest.mark.asyncio
async def test_db_tuple_delete(session: AsyncSession) -> None:
    store = await DbTupleStore.load(session)
    t = RelationTuple.of("book:1", "owner", "user:alice")
    await store.awrite(t)
    await session.commit()
    await store.adelete(t)
    await session.commit()
    reloaded = await DbTupleStore.load(session)
    assert len(reloaded) == 0


@pytest.mark.asyncio
async def test_db_decision_log_flush(session: AsyncSession) -> None:
    from app.platform.authz import Resource, build_plane

    # A plane that logs to the DB sink (via a session factory over the same URL).
    assert _URL is not None
    engine = create_async_engine(_URL, poolclass=NullPool)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    sink = DbDecisionLog(maker)
    plane = build_plane(decision_log=sink, include_auth_rbac=False)

    plane.check_sync("alice", "book:edit", Resource.of("book", "1", owner="alice"))
    plane.check_sync("bob", "book:edit", Resource.of("book", "1", owner="alice"))
    assert sink.pending == 2
    written = await sink.flush()
    assert written == 2
    assert sink.pending == 0

    # the rows are queryable back
    from app.platform.authz.store_db import load_persisted_records

    async with maker() as s:
        records = await load_persisted_records(s)
    assert len(records) >= 2
    await engine.dispose()
