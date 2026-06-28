"""Integration tests for ReportService + the read-only sources (need infra).

Exercises the service path against a real session: seed books/shots/sessions,
then generate reader + operator reports and assert the aggregated numbers flow
through into the rendered model. Skips cleanly without the throwaway infra.
"""

from __future__ import annotations

import pytest

from app.api.security import hash_password
from app.composition import Container
from app.db.base import new_id
from app.db.models.enums import BookStatus, ShotStatus
from app.db.models.session import Session
from app.db.models.shot import Shot, SourceSpanIndex
from app.db.models.user import User
from app.db.repositories.book import BookRepo
from app.reports.db_model import ReportKind
from app.reports.render import ReportFormat
from app.reports.service import ReportRequest, ReportService
from app.reports.sources import OperatorSource, ReaderSource
from app.reports.storage import ReportArtifactStore

pytestmark = pytest.mark.asyncio


async def _seed_reading(container: Container, user_id: str) -> str:
    """Seed a book with shots, a source-span index, and a reading session."""
    book_id = new_id()
    async with container.session_factory() as session:
        await BookRepo(session).create(
            title="Read Me",
            author="Author",
            book_id=book_id,
            user_id=user_id,
            status=BookStatus.READY,
            num_pages=10,
        )
        # 4 shots, 3 accepted.
        first_shot_id = new_id()
        for i in range(4):
            session.add(
                Shot(
                    id=first_shot_id if i == 0 else new_id(),
                    book_id=book_id,
                    scene_id="scene_001",
                    status=ShotStatus.ACCEPTED if i < 3 else ShotStatus.PLANNED,
                    duration_s=5.0,
                    qa={"ccs": 0.9, "regens": 1} if i == 0 else {},
                )
            )
        await session.flush()
        session.add(
            SourceSpanIndex(
                id=new_id(),
                book_id=book_id,
                word_index_start=0,
                word_index_end=1000,
                shot_id=first_shot_id,
                scene_id="scene_001",
            )
        )
        session.add(
            Session(
                id=new_id(),
                user_id=user_id,
                book_id=book_id,
                focus_word=600,
                last_activity_ms=1_700_000_000_000,
            )
        )
        await session.commit()
    return book_id


async def _user_id(container: Container) -> str:
    uid = new_id()
    async with container.session_factory() as session:
        session.add(
            User(id=uid, email=f"{uid}@example.com", hashed_password=hash_password("x"))
        )
        await session.commit()
    return uid


def _service(container: Container) -> ReportService:
    return ReportService(artifact_store=ReportArtifactStore(container.object_store))


async def test_reader_source_aggregates_progress(container: Container) -> None:
    uid = await _user_id(container)
    book_id = await _seed_reading(container, uid)
    async with container.session_factory() as session:
        progress = await ReaderSource(session).book_progress(book_id, uid)
    assert progress is not None
    assert progress.total_words == 1000
    assert progress.furthest_word == 600
    assert progress.percent_complete == pytest.approx(0.6)
    assert progress.accepted_shots == 3
    assert progress.total_shots == 4
    assert progress.watched_seconds == pytest.approx(15.0)


async def test_operator_quality_source_aggregates(container: Container) -> None:
    uid = await _user_id(container)
    book_id = await _seed_reading(container, uid)
    async with container.session_factory() as session:
        snap = await OperatorSource(session).quality_snapshot(book_id=book_id)
    assert snap.total_shots == 4
    assert snap.accepted_shots == 3
    assert snap.total_video_seconds == pytest.approx(20.0)
    assert snap.accepted_video_seconds == pytest.approx(15.0)
    assert snap.regen_count == 1
    assert snap.mean_ccs == pytest.approx(0.9)


async def test_service_generates_and_persists_reading_progress(container: Container) -> None:
    uid = await _user_id(container)
    book_id = await _seed_reading(container, uid)
    service = _service(container)
    req = ReportRequest(
        kind=ReportKind.READING_PROGRESS,
        fmt=ReportFormat.JSON,
        user_id=uid,
        book_id=book_id,
        reader_name="Tester",
    )
    async with container.session_factory() as session:
        result = await service.generate(session, req)
        await session.commit()
    assert result.artifact.status.value == "ready"
    assert result.artifact.storage_key
    assert result.download_url
    # The stored bytes are the JSON model and decode to the report.
    assert b'"reading_progress"' in result.data


async def test_service_dedups_identical_report(container: Container) -> None:
    uid = await _user_id(container)
    book_id = await _seed_reading(container, uid)
    service = _service(container)
    req = ReportRequest(
        kind=ReportKind.READING_PROGRESS,
        fmt=ReportFormat.JSON,
        user_id=uid,
        book_id=book_id,
        reader_name="Tester",
    )
    async with container.session_factory() as session:
        first = await service.generate(session, req)
        await session.commit()
    async with container.session_factory() as session:
        second = await service.generate(session, req)
        await session.commit()
    assert second.deduped is True
    assert second.artifact.id == first.artifact.id


async def test_service_builds_operator_budget_report(container: Container) -> None:
    service = _service(container)
    req = ReportRequest(kind=ReportKind.BUDGET, fmt=ReportFormat.HTML)
    async with container.session_factory() as session:
        report = await service.build_report(session, req)
    assert report.meta.kind == "budget"
