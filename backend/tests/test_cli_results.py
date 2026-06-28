"""Pure rendering tests for every action result dataclass (no infra).

Each result type must (a) produce JSON-serializable ``data`` and (b) render a
non-empty table. These tests instantiate every result directly — no DB — so the
rendering contract is verified independently of the data-access layer.
"""

from __future__ import annotations

import json

from app.cli.actions.books import ActionResult, BookDetail, BookList, BookRow
from app.cli.actions.budget import (
    BudgetReport,
    CapsReport,
    EfficiencyReport,
    LedgerEntry,
    LedgerTail,
    PerBookSpend,
    RemainingReport,
)
from app.cli.actions.canon import (
    AuditVerifyReport,
    BranchListing,
    BranchRow,
    EntityListing,
    EntityRow,
    IntegrityIssue,
    IntegrityReport,
    StateListing,
    StateRow,
)
from app.cli.actions.doctor import Check, DoctorReport
from app.cli.actions.maintenance import (
    CacheAuditReport,
    CensusReport,
    EmbeddingCoverageReport,
    StuckImportReport,
    StuckImportRow,
)
from app.cli.actions.queue import DlqList, JobDetail, OpResult, QueueStatsReport
from app.cli.actions.render_jobs import (
    DefectListing,
    DefectRow,
    JobListing,
    JobMirror,
    JobRow,
)
from app.cli.actions.users import (
    OrphanReport,
    ReassignResult,
    UserDetail,
    UserList,
    UserRow,
)
from app.cli.errors import EXIT_OK, EXIT_UNAVAILABLE
from app.cli.output import render_json, render_table
from app.db.models.enums import RenderJobStatus, RenderPriority
from app.queue.redis_queue import QueuedJob


def _assert_renders(result: object) -> dict:
    payload = result.render_payload()  # type: ignore[attr-defined]
    text = render_json(payload)
    data = json.loads(text)  # must be JSON-serializable
    assert render_table(payload)  # table mode never empty
    return data


def test_doctor_report_healthy_vs_failing() -> None:
    healthy = DoctorReport(
        checks=(Check("postgres", True, "ok", 1.0), Check("redis", True, "ok", 2.0)),
        counts={"books": 3},
        budget_remaining_s=1000.0,
        budget_ceiling_s=1650.0,
        live_video=False,
    )
    data = _assert_renders(healthy)
    assert data["healthy"] is True
    assert healthy.exit_code == EXIT_OK

    failing = DoctorReport(checks=(Check("redis", False, "down", None),))
    assert failing.healthy is False
    assert failing.exit_code == EXIT_UNAVAILABLE
    fdata = _assert_renders(failing)
    assert fdata["checks"][0]["ok"] is False


def test_book_results() -> None:
    row = BookRow(
        id="b1",
        title="Tale",
        author="A",
        status="ready",
        num_pages=12,
        user_id="u1",
        created_at_iso="2026-01-01T00:00:00+00:00",
        created_ago="1d ago",
    )
    data = _assert_renders(BookList(books=(row,), total=1, status_filter="ready"))
    assert data["books"][0]["id"] == "b1"

    detail = BookDetail(
        id="b1",
        title="Tale",
        author=None,
        status="ready",
        num_pages=12,
        user_id="u1",
        source_pdf_key="src/b1.pdf",
        cover_key=None,
        created_at_iso=None,
        page_count=12,
        scene_count=4,
        shot_count=10,
        shots_accepted=7,
        defect_count=1,
        budget_committed_s=35.0,
        budget_reserved_s=5.0,
        shot_status_breakdown={"accepted": 7, "planned": 3},
    )
    ddata = _assert_renders(detail)
    assert ddata["counts"]["shots_accepted"] == 7
    assert ddata["budget"]["committed_s"] == 35.0

    res = ActionResult(ok=True, action="delete", detail={"book_id": "b1"}, message="done")
    rdata = _assert_renders(res)
    assert rdata["ok"] is True


def test_budget_results() -> None:
    report = BudgetReport(
        ceiling_s=1650.0,
        committed_s=400.0,
        reserved_s=50.0,
        remaining_s=1200.0,
        low_floor_s=120.0,
        is_low=False,
        live_video=False,
        per_book=(PerBookSpend(book_id="b1", title="Tale", committed_s=400.0),),
    )
    data = _assert_renders(report)
    assert data["used_s"] == 450.0
    assert report.used_s == 450.0

    _assert_renders(
        RemainingReport(remaining_s=10.0, ceiling_s=1650.0, is_low=True, live_video=True)
    )
    _assert_renders(
        LedgerTail(
            entries=(
                LedgerEntry(
                    id="e1",
                    kind="commit",
                    video_seconds=5.0,
                    reservation_id="r1",
                    book_id="b1",
                    session_id=None,
                    scene_id=None,
                    note="ok",
                    created_at_iso=None,
                ),
            ),
            scope={"book_id": "b1", "session_id": None, "scene_id": None},
        )
    )

    from app.memory.budget_service import BudgetLimits

    limits = BudgetLimits(
        ceiling_video_s=1650.0,
        per_session_s=300.0,
        per_scene_s=90.0,
        low_floor_s=120.0,
        live_video=False,
    )
    _assert_renders(CapsReport(limits=limits))

    eff = EfficiencyReport(
        book_id="b1",
        accepted_seconds=80.0,
        committed_seconds=100.0,
        accepted_shots=16,
        total_committed_shots=20,
    )
    edata = _assert_renders(eff)
    assert edata["efficiency_pct"] == 80.0
    # Zero committed => undefined efficiency.
    zero = EfficiencyReport(
        book_id=None,
        accepted_seconds=0.0,
        committed_seconds=0.0,
        accepted_shots=0,
        total_committed_shots=0,
    )
    assert zero.efficiency_pct is None
    _assert_renders(zero)


def test_queue_results() -> None:
    stats = QueueStatsReport(
        depths={"committed": 2, "speculative": 1, "keyframe": 0},
        processing=1,
        dlq=3,
        inflight={"committed": 1, "speculative": 0, "keyframe": 0},
        enqueued_total=10,
        succeeded_total=5,
        dropped_total=1,
        deadletter_total=3,
        cancelled_total=0,
    )
    data = _assert_renders(stats)
    assert data["total_queued"] == 3
    assert stats.total_queued == 3

    job = QueuedJob(
        id="j1",
        shot_hash="h1",
        priority=RenderPriority.COMMITTED,
        status=RenderJobStatus.DEADLETTER,
        book_id="b1",
        attempts=3,
        error="boom",
    )
    _assert_renders(JobDetail(job=job))
    _assert_renders(DlqList(jobs=(job,), job_ids=("j1",)))
    _assert_renders(OpResult(ok=True, action="replay", detail={"new_job_id": "j2"}, message="ok"))


def test_canon_results() -> None:
    listing = EntityListing(
        book_id="b1",
        beat=10,
        kind="character",
        entities=(
            EntityRow(
                entity_key="char_hero",
                type="character",
                name="Hero",
                version=2,
                valid_from_beat=1,
                valid_to_beat=None,
                has_embedding=True,
                description="brave",
            ),
        ),
    )
    data = _assert_renders(listing)
    assert data["entities"][0]["entity_key"] == "char_hero"

    _assert_renders(
        StateListing(
            book_id="b1",
            states=(
                StateRow(
                    subject_entity_key="char_hero",
                    predicate="possesses",
                    object_value="sword",
                    valid_from_beat=12,
                    valid_to_beat=34,
                    version=1,
                ),
            ),
        )
    )

    ok = AuditVerifyReport(book_id="b1", length=5, valid=True, first_break_seq=None, detail="ok")
    odata = _assert_renders(ok)
    assert odata["valid"] is True
    broken = AuditVerifyReport(
        book_id="b1", length=5, valid=False, first_break_seq=3, detail="break"
    )
    bdata = _assert_renders(broken)
    assert bdata["first_break_seq"] == 3

    _assert_renders(
        BranchListing(
            book_id="b1",
            branches=(
                BranchRow(
                    name="main",
                    parent=None,
                    status="open",
                    base_beat=0,
                    created_at_iso=None,
                    note=None,
                ),
            ),
        )
    )

    healthy = IntegrityReport(book_id="b1", checked={"entities": 3}, issues=())
    assert healthy.ok is True
    issues = IntegrityReport(
        book_id="b1",
        checked={"entities": 3},
        issues=(IntegrityIssue("error", "inverted_entity_interval", "char@v1", "to < from"),),
    )
    assert issues.ok is False
    idata = _assert_renders(issues)
    assert idata["issues"][0]["kind"] == "inverted_entity_interval"


def test_user_results() -> None:
    users = UserList(
        users=(
            UserRow(
                id="u1",
                email="a@b.c",
                book_count=2,
                created_at_iso=None,
                created_ago="1d ago",
            ),
        ),
        total=1,
    )
    _assert_renders(users)
    detail = UserDetail(
        id="u1",
        email="a@b.c",
        created_at_iso=None,
        book_count=1,
        books=(("b1", "Tale", "ready"),),
    )
    data = _assert_renders(detail)
    assert data["books"][0]["id"] == "b1"
    _assert_renders(OrphanReport(books=(("b2", "Orphan", "failed"),), total=1))
    _assert_renders(ReassignResult(book_id="b1", from_user="u1", to_user="u2"))


def test_render_job_results() -> None:
    jobs = JobListing(
        jobs=(
            JobRow(
                id="j1",
                priority="committed",
                status="succeeded",
                shot_id="s1",
                session_id=None,
                attempts=0,
                reserved_video_s=5.0,
                created_ago="1m ago",
            ),
        ),
        total=1,
        status_filter=None,
    )
    _assert_renders(jobs)
    mirror = JobMirror(
        id="j1",
        priority="committed",
        status="succeeded",
        shot_id="s1",
        shot_hash="h1",
        session_id=None,
        cancel_token=None,
        attempts=0,
        provider_task_id=None,
        error=None,
        reserved_video_s=5.0,
        created_at_iso=None,
        updated_at_iso=None,
    )
    _assert_renders(mirror)
    defects = DefectListing(
        book_id="b1",
        defects=(
            DefectRow(
                id="d1",
                kind="qa_fail",
                shot_id="s1",
                created_at_iso="2026-01-01T00:00:00+00:00",
                detail={"score": 0.2},
            ),
        ),
    )
    _assert_renders(defects)


def test_maintenance_results() -> None:
    _assert_renders(CensusReport(counts={"books": 3, "shots": 10}))
    stuck = StuckImportReport(
        books=(StuckImportRow(book_id="b1", title="Tale", has_source=True),),
        spawned=1,
    )
    data = _assert_renders(stuck)
    assert data["spawned"] == 1
    _assert_renders(
        CacheAuditReport(total_rows=10, with_clip=7, without_clip=3, cached_video_seconds=35.0)
    )
    cov = EmbeddingCoverageReport(
        accepted_shots=10,
        accepted_with_embedding=8,
        entities=5,
        entities_with_embedding=5,
        scope={"book_id": None},
    )
    cdata = _assert_renders(cov)
    assert cdata["shot_embedding_pct"] == 0.8
