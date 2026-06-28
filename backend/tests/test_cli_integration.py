"""Integration tests for the DB/queue-backed CLI actions (isolated infra).

These exercise the *real* action functions against the wired
:class:`~app.composition.Container` (real Postgres + Redis + object store), the
same way the API tests do — so the CLI is verified end-to-end, not just its
rendering. They SKIP cleanly unless the throwaway-infra env vars are set, and
(per the project's isolation rule) must point at a throwaway DB + redis db 15,
never the live ``kinora`` database.

The autouse ``_isolate_state`` fixture in conftest TRUNCATEs every table and
FLUSHes Redis before each test, so each test starts from a clean slate.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio

from app.composition import Container, build_container
from app.core.config import Settings
from app.db.base import new_id
from app.db.models.bitemporal import AuditAction
from app.db.models.enums import (
    BookStatus,
    EntityType,
    RenderJobStatus,
    RenderPriority,
    ShotStatus,
)
from app.db.repositories.bitemporal import CanonAuditRepo
from app.db.repositories.book import BookRepo
from app.db.repositories.continuity import ContinuityStateRepo
from app.db.repositories.entity import EntityRepo
from app.db.repositories.user import UserRepo

_DB_URL = os.environ.get("KINORA_TEST_DATABASE_URL")
_REDIS_URL = os.environ.get("KINORA_TEST_REDIS_URL")
_S3_ENDPOINT = os.environ.get("KINORA_TEST_S3_ENDPOINT_URL") or os.environ.get(
    "KINORA_TEST_S3_ENDPOINT"
)

pytestmark = pytest.mark.skipif(
    not (_DB_URL and _REDIS_URL and _S3_ENDPOINT),
    reason="CLI integration tests require KINORA_TEST_DATABASE_URL + _REDIS_URL + _S3_ENDPOINT_URL",
)


def _settings() -> Settings:
    assert _DB_URL and _REDIS_URL and _S3_ENDPOINT
    return Settings(
        dashscope_api_key="test",
        app_env="local",
        jwt_secret="kinora-test-jwt-secret-key-which-is-comfortably-32-bytes",
        database_url=_DB_URL,
        redis_url=_REDIS_URL,
        s3_endpoint_url=_S3_ENDPOINT,
        s3_access_key=os.environ.get("KINORA_TEST_S3_ACCESS_KEY", "kinora"),
        s3_secret_key=os.environ.get("KINORA_TEST_S3_SECRET_KEY", "kinora-secret"),
        s3_region=os.environ.get("KINORA_TEST_S3_REGION", "us-east-1"),
        s3_bucket=os.environ.get("KINORA_TEST_S3_BUCKET", "kinora"),
        kinora_live_video=False,
        budget_ceiling_video_s=300.0,
        budget_per_session_s=120.0,
        budget_per_scene_s=60.0,
        budget_low_floor_s=30.0,
    )


@pytest_asyncio.fixture
async def cli_container() -> AsyncIterator[Container]:
    c = build_container(_settings())
    c.object_store.ensure_bucket()
    try:
        yield c
    finally:
        await c.queue.purge()
        await c.shutdown()


# --------------------------------------------------------------------------- #
# doctor
# --------------------------------------------------------------------------- #


async def test_doctor_healthy_against_live_infra(cli_container: Container) -> None:
    from app.cli.actions.doctor import run_doctor

    report = await run_doctor(cli_container)
    by_name = {c.name: c for c in report.checks}
    assert by_name["postgres"].ok
    assert by_name["redis"].ok
    assert by_name["render_queue"].ok
    assert by_name["object_store"].ok
    assert report.healthy
    assert report.budget_remaining_s == pytest.approx(300.0)
    assert report.counts["books"] == 0  # clean slate


# --------------------------------------------------------------------------- #
# books
# --------------------------------------------------------------------------- #


async def _make_book(container: Container, **kw: object) -> str:
    book_id = str(kw.pop("book_id", None) or new_id())
    async with container.session_factory() as db:
        await BookRepo(db).create(book_id=book_id, **{"title": "Tale", **kw})  # type: ignore[arg-type]
    return book_id


async def test_books_list_inspect_set_status(cli_container: Container) -> None:
    from app.cli.actions import books as actions

    b1 = await _make_book(cli_container, title="Alpha", status=BookStatus.READY, num_pages=10)
    await _make_book(cli_container, title="Beta", status=BookStatus.IMPORTING)

    listing = await actions.list_books(cli_container)
    assert listing.total == 2
    titles = {b.title for b in listing.books}
    assert {"Alpha", "Beta"} == titles

    ready_only = await actions.list_books(cli_container, status=BookStatus.READY)
    assert ready_only.total == 1
    assert ready_only.books[0].title == "Alpha"

    detail = await actions.inspect_book(cli_container, b1)
    assert detail.page_count == 0
    assert detail.shot_count == 0

    res = await actions.set_book_status(cli_container, b1, BookStatus.FAILED)
    assert res.detail["to"] == "failed"
    after = await actions.inspect_book(cli_container, b1)
    assert after.status == "failed"


async def test_books_inspect_not_found(cli_container: Container) -> None:
    from app.cli.actions import books as actions
    from app.cli.errors import CliError

    with pytest.raises(CliError):
        await actions.inspect_book(cli_container, "nope")


async def test_books_delete_removes_row(cli_container: Container) -> None:
    from app.cli.actions import books as actions
    from app.cli.errors import CliError

    b1 = await _make_book(cli_container, title="Gone")
    res = await actions.delete_book(cli_container, b1, purge_storage=False)
    assert res.ok
    with pytest.raises(CliError):
        await actions.inspect_book(cli_container, b1)


async def test_books_reingest_requires_source(cli_container: Container) -> None:
    from app.cli.actions import books as actions
    from app.cli.errors import CliError

    b1 = await _make_book(cli_container, title="NoSource")
    with pytest.raises(CliError):
        await actions.reingest_book(cli_container, b1)


# --------------------------------------------------------------------------- #
# budget
# --------------------------------------------------------------------------- #


async def test_budget_report_reflects_reservations(cli_container: Container) -> None:
    from app.cli.actions import budget as actions
    from app.db.repositories.budget import BudgetRepo
    from app.memory.budget_service import BudgetService

    b1 = await _make_book(cli_container, title="Spendy")
    async with cli_container.session_factory() as db:
        service = BudgetService(repo=BudgetRepo(db), limits=cli_container.budget_limits)
        reservation = await service.reserve(20.0, book_id=b1)
        await service.commit(reservation, 18.0)

    report = await actions.budget_report(cli_container)
    assert report.committed_s == pytest.approx(18.0)
    assert report.remaining_s == pytest.approx(300.0 - 18.0)
    assert any(b.book_id == b1 and b.committed_s == pytest.approx(18.0) for b in report.per_book)

    remaining = await actions.budget_remaining(cli_container)
    assert remaining.remaining_s == pytest.approx(282.0)

    ledger = await actions.budget_ledger(cli_container, book_id=b1)
    kinds = {e.kind for e in ledger.entries}
    assert {"reserve", "commit"} <= kinds

    caps = actions.budget_caps(cli_container)
    assert caps.limits.ceiling_video_s == 300.0


async def test_budget_efficiency(cli_container: Container) -> None:
    from app.cli.actions import budget as actions
    from app.db.repositories.budget import BudgetRepo
    from app.db.repositories.shot import ShotRepo
    from app.memory.budget_service import BudgetService

    b1 = await _make_book(cli_container, title="Eff")
    async with cli_container.session_factory() as db:
        await ShotRepo(db).create(
            id=new_id(), book_id=b1, status=ShotStatus.ACCEPTED, duration_s=8.0
        )
        await ShotRepo(db).create(
            id=new_id(), book_id=b1, status=ShotStatus.DEGRADED, duration_s=5.0
        )
        service = BudgetService(repo=BudgetRepo(db), limits=cli_container.budget_limits)
        reservation = await service.reserve(10.0, book_id=b1)
        await service.commit(reservation, 10.0)

    eff = await actions.budget_efficiency(cli_container, book_id=b1)
    assert eff.accepted_seconds == pytest.approx(8.0)
    assert eff.committed_seconds == pytest.approx(10.0)
    assert eff.efficiency_pct == pytest.approx(80.0)


# --------------------------------------------------------------------------- #
# queue + DLQ replay
# --------------------------------------------------------------------------- #


async def test_queue_stats_and_dlq_replay(cli_container: Container) -> None:
    from app.cli.actions import queue as actions

    queue = cli_container.queue
    # Enqueue + force-deadletter a committed job via repeated retries.
    job_id = new_id()
    result = await queue.enqueue(
        shot_hash="hash-dlq",
        priority=RenderPriority.COMMITTED,
        book_id="book-x",
        job_id=job_id,
        target_duration_s=5.0,
    )
    assert result.created
    claimed = await queue.claim()
    assert claimed is not None
    # retry_cap defaults to 2 -> 3rd failure dead-letters.
    for _ in range(3):
        outcome = await queue.retry(job_id, error="boom")
    assert outcome.decision.value == "deadletter"

    stats = await actions.queue_stats(queue)
    assert stats.dlq >= 1

    dlq = await actions.list_dlq(queue)
    assert job_id in dlq.job_ids

    replay = await actions.replay_job(queue, job_id)
    assert replay.ok
    assert replay.detail["new_job_id"] != job_id

    # Re-enqueued under a fresh id, the new job is claimable again.
    new_job_id = str(replay.detail["new_job_id"])
    fetched = await queue.get_job(new_job_id)
    assert fetched is not None
    assert fetched.status is RenderJobStatus.QUEUED


async def test_queue_inspect_not_found(cli_container: Container) -> None:
    from app.cli.actions import queue as actions
    from app.cli.errors import CliError

    with pytest.raises(CliError):
        await actions.inspect_job(cli_container.queue, "missing")


async def test_queue_cancel_token(cli_container: Container) -> None:
    from app.cli.actions import queue as actions

    queue = cli_container.queue
    token = "traj-1"
    await queue.enqueue(
        shot_hash="h-cancel",
        priority=RenderPriority.SPECULATIVE,
        book_id="book-y",
        job_id=new_id(),
        cancel_token=token,
    )
    res = await actions.cancel_token(queue, token)
    cancelled = res.detail["cancelled"]
    assert isinstance(cancelled, int) and cancelled >= 1


# --------------------------------------------------------------------------- #
# canon
# --------------------------------------------------------------------------- #


async def test_canon_entities_states_and_audit(cli_container: Container) -> None:
    from app.cli.actions import canon as actions

    b1 = await _make_book(cli_container, title="Canon")
    async with cli_container.session_factory() as db:
        repo = EntityRepo(db)
        await repo.upsert_new_version(
            book_id=b1,
            entity_key="char_hero",
            entity_type=EntityType.CHARACTER,
            name="Hero",
            valid_from_beat=1,
        )
        await repo.upsert_new_version(
            book_id=b1,
            entity_key="char_hero",
            entity_type=EntityType.CHARACTER,
            name="Hero (older)",
            valid_from_beat=10,
        )
        await ContinuityStateRepo(db).assert_state(
            book_id=b1,
            subject_entity_key="char_hero",
            predicate="possesses",
            object_value="sword",
            valid_from_beat=2,
        )

    at_beat = await actions.list_entities(cli_container, b1, beat=12)
    assert len(at_beat.entities) == 1
    assert at_beat.entities[0].version == 2

    versions = await actions.list_entities(cli_container, b1, entity_key="char_hero")
    assert len(versions.entities) == 2

    states = await actions.list_states(cli_container, b1)
    assert len(states.states) == 1
    assert states.states[0].predicate == "possesses"

    integrity = await actions.check_integrity(cli_container, b1)
    assert integrity.ok  # contiguous versions, valid intervals


async def test_canon_audit_verify_detects_intact_and_break(cli_container: Container) -> None:
    from app.cli.actions import canon as actions

    b1 = await _make_book(cli_container, title="Audit")
    async with cli_container.session_factory() as db:
        repo = CanonAuditRepo(db)
        for value in (1, 2):
            await repo.append(
                book_id=b1,
                branch="main",
                action=AuditAction.ASSERT_FACT,
                actor_id="tester",
                target_key="char_hero",
                payload={"x": value},
                payload_repr=repr({"x": value}),
            )

    verify = await actions.verify_audit_chain(cli_container, b1)
    assert verify.valid
    assert verify.length == 2

    # Tamper with a row's payload so the recomputed hash no longer matches.
    from sqlalchemy import update

    from app.db.models.bitemporal import CanonAudit

    async with cli_container.session_factory() as db:
        await db.execute(
            update(CanonAudit)
            .where(CanonAudit.book_id == b1, CanonAudit.seq == 2)
            .values(payload={"tampered": True})
        )

    broken = await actions.verify_audit_chain(cli_container, b1)
    assert not broken.valid
    assert broken.first_break_seq == 2


async def test_canon_integrity_flags_inverted_interval(cli_container: Container) -> None:
    from sqlalchemy import update

    from app.cli.actions import canon as actions
    from app.db.models.entity import Entity

    b1 = await _make_book(cli_container, title="Bad")
    async with cli_container.session_factory() as db:
        await EntityRepo(db).upsert_new_version(
            book_id=b1,
            entity_key="char_x",
            entity_type=EntityType.CHARACTER,
            name="X",
            valid_from_beat=10,
        )
        # Force an inverted interval.
        await db.execute(
            update(Entity)
            .where(Entity.book_id == b1, Entity.entity_key == "char_x")
            .values(valid_to_beat=5)
        )

    report = await actions.check_integrity(cli_container, b1)
    assert not report.ok
    assert any(i.kind == "inverted_entity_interval" for i in report.issues)


# --------------------------------------------------------------------------- #
# users
# --------------------------------------------------------------------------- #


async def test_users_list_inspect_reassign_orphans(cli_container: Container) -> None:
    from app.cli.actions import users as actions
    from app.cli.errors import CliError

    async with cli_container.session_factory() as db:
        u1 = await UserRepo(db).create(email="a@b.c", hashed_password="x")
        u2 = await UserRepo(db).create(email="d@e.f", hashed_password="y")
    owned = await _make_book(cli_container, title="Owned", user_id=u1.id)
    orphan = await _make_book(cli_container, title="Orphan")

    listing = await actions.list_users(cli_container)
    assert listing.total == 2
    by_email = {u.email: u for u in listing.users}
    assert by_email["a@b.c"].book_count == 1

    detail = await actions.inspect_user(cli_container, email="a@b.c")
    assert detail.book_count == 1
    assert detail.books[0][0] == owned

    orphans = await actions.list_orphan_books(cli_container)
    assert orphans.total == 1
    assert orphans.books[0][0] == orphan

    res = await actions.reassign_book(cli_container, orphan, u2.id)
    assert res.from_user is None
    assert res.to_user == u2.id
    orphans_after = await actions.list_orphan_books(cli_container)
    assert orphans_after.total == 0

    with pytest.raises(CliError):
        await actions.inspect_user(cli_container, user_id="ghost")


# --------------------------------------------------------------------------- #
# render jobs + maintenance
# --------------------------------------------------------------------------- #


async def test_render_jobs_mirror_and_defects(cli_container: Container) -> None:
    from app.cli.actions import render_jobs as actions
    from app.db.repositories.defect import DefectRepo
    from app.db.repositories.render_job import RenderJobRepo

    b1 = await _make_book(cli_container, title="Defective")
    async with cli_container.session_factory() as db:
        job = await RenderJobRepo(db).create(
            priority=RenderPriority.COMMITTED,
            status=RenderJobStatus.SUCCEEDED,
            shot_hash="h-mirror",
        )
        job_id = job.id
        await DefectRepo(db).log(book_id=b1, kind="qa_fail", detail={"score": 0.1})

    jobs = await actions.list_jobs(cli_container)
    assert jobs.total >= 1
    mirror = await actions.inspect_job_mirror(cli_container, job_id)
    assert mirror.status == "succeeded"

    defects = await actions.list_defects(cli_container, b1)
    assert len(defects.defects) == 1
    assert defects.defects[0].kind == "qa_fail"


async def test_maintenance_census_cache_embedding(cli_container: Container) -> None:
    from app.cli.actions import maintenance as actions
    from app.db.repositories.shot import ShotCacheRepo

    b1 = await _make_book(cli_container, title="Maint")
    async with cli_container.session_factory() as db:
        await ShotCacheRepo(db).put(
            shot_hash="cache-1", book_id=b1, clip_key="clips/x.mp4", video_seconds=5.0
        )
        await ShotCacheRepo(db).put(shot_hash="cache-2", book_id=b1, video_seconds=0.0)

    census = await actions.census(cli_container)
    assert census.counts["books"] == 1
    assert census.counts["shot_cache"] == 2

    cache = await actions.cache_audit(cli_container, book_id=b1)
    assert cache.total_rows == 2
    assert cache.with_clip == 1
    assert cache.cached_video_seconds == pytest.approx(5.0)

    cov = await actions.embedding_coverage(cli_container, book_id=b1)
    assert cov.accepted_shots == 0


async def test_maintenance_stuck_imports_report_only(cli_container: Container) -> None:
    from app.cli.actions import maintenance as actions

    await _make_book(cli_container, title="Stuck", status=BookStatus.IMPORTING)
    report = await actions.stuck_imports(cli_container, respawn=False)
    assert len(report.books) == 1
    assert report.spawned is None  # report-only
    assert report.books[0].has_source is False
