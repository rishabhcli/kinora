"""The report service — orchestrate build → render → store → index.

The single entry point for *generating* a report. It wires the read-only data
seams (:mod:`app.reports.sources`) to the pure builders
(:mod:`app.reports.builders`), renders the resulting :class:`Report` to the
requested format (:mod:`app.reports.render`), persists the bytes
(:mod:`app.reports.storage`), and writes the :class:`ReportArtifact` index row
(:mod:`app.reports.repository`). It is the same path for **on-demand** (an API
request) and **scheduled** (a periodic job) generation — the only difference is
the ``trigger`` recorded on the row.

Determinism + dedup: the service hashes the rendered bytes and, when an identical
artifact already exists, reuses it instead of writing a new object. That makes a
re-request of the same report cheap and idempotent (the §8.7 idea applied to
documents).

A :class:`ReportRequest` is the typed description of *what* to generate; the
service resolves it against the database and brand, builds, renders, and returns
a :class:`GeneratedReport` (the row + the bytes + a signed URL). Everything is
read-only against the rest of the system and spends **zero video-seconds**.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.core.logging import get_logger
from app.db.repositories.book import BookRepo
from app.reports.builders import (
    build_budget_report,
    build_completion_certificate,
    build_highlights_digest,
    build_library_overview_report,
    build_quality_report,
    build_reading_progress_report,
    build_throughput_report,
    build_year_in_review,
)
from app.reports.db_model import (
    ReportArtifact,
    ReportAudience,
    ReportFormatEnum,
    ReportKind,
    ReportStatus,
)
from app.reports.model import Report
from app.reports.render import ReportFormat, render
from app.reports.repository import ReportArtifactRepo
from app.reports.sources import OperatorSource, QualitySnapshot, ReaderSource
from app.reports.storage import ReportArtifactStore, content_hash
from app.reports.theme import Brand, certificate_brand, default_brand

logger = get_logger("app.reports.service")

#: Which kinds are reader-facing vs operator-facing (audience gate).
READER_KINDS = frozenset(
    {
        ReportKind.READING_PROGRESS,
        ReportKind.COMPLETION_CERTIFICATE,
        ReportKind.YEAR_IN_REVIEW,
        ReportKind.HIGHLIGHTS_DIGEST,
    }
)
OPERATOR_KINDS = frozenset(
    {
        ReportKind.BUDGET,
        ReportKind.QUALITY,
        ReportKind.RENDER_THROUGHPUT,
        ReportKind.LIBRARY_OVERVIEW,
    }
)


def audience_for(kind: ReportKind) -> ReportAudience:
    """The audience a report kind belongs to."""
    return ReportAudience.READER if kind in READER_KINDS else ReportAudience.OPERATOR


@dataclass(frozen=True, slots=True)
class ReportRequest:
    """A typed description of a report to generate."""

    kind: ReportKind
    fmt: ReportFormat = ReportFormat.PDF
    user_id: str | None = None
    book_id: str | None = None
    reader_name: str | None = None
    year: int | None = None
    trigger: str = "on_demand"
    #: Operator scope: budget ceiling override + optional baseline arm flag.
    params: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class GeneratedReport:
    """The result of generating a report: index row + bytes + signed URL."""

    artifact: ReportArtifact
    data: bytes
    download_url: str
    deduped: bool


class ReportGenerationError(Exception):
    """Raised when a report cannot be built (missing subject / bad request)."""


class ReportService:
    """Build, render, store, and index reports on demand or on a schedule."""

    def __init__(
        self,
        *,
        artifact_store: ReportArtifactStore,
        brand: Brand | None = None,
        url_ttl: int = 3600,
        ceiling_seconds: float = 1650.0,
    ) -> None:
        self._store = artifact_store
        self._brand = brand or default_brand()
        self._ttl = url_ttl
        self._ceiling = ceiling_seconds

    # ----------------------------------------------------------------- build #

    async def build_report(self, session: Any, req: ReportRequest) -> Report:
        """Resolve a request against the DB and return the built (unrendered) report.

        Pure-ish: reads through the read-only sources, then calls a pure builder.
        Separated from rendering so a caller can preview the model (e.g. the JSON
        contract) without materialising a stored artifact.
        """
        now = datetime.now(UTC)
        if req.kind in READER_KINDS:
            return await self._build_reader(session, req, now)
        return await self._build_operator(session, req, now)

    async def _build_reader(self, session: Any, req: ReportRequest, now: datetime) -> Report:
        src = ReaderSource(session)
        if req.kind in (ReportKind.READING_PROGRESS, ReportKind.COMPLETION_CERTIFICATE):
            if not req.book_id or not req.user_id:
                raise ReportGenerationError("book_id and user_id are required")
            progress = await src.book_progress(req.book_id, req.user_id)
            if progress is None:
                raise ReportGenerationError("no such book for this reader")
            if req.kind is ReportKind.READING_PROGRESS:
                return build_reading_progress_report(
                    progress, generated_at=now, reader_name=req.reader_name
                )
            if not progress.is_complete:
                raise ReportGenerationError("book is not complete yet")
            return build_completion_certificate(
                progress,
                generated_at=now,
                reader_name=req.reader_name,
                certificate_no=req.params.get("certificate_no"),
            )
        # Whole-library reader reports.
        if not req.user_id:
            raise ReportGenerationError("user_id is required")
        summary = await src.reader_summary(req.user_id)
        if req.kind is ReportKind.YEAR_IN_REVIEW:
            return build_year_in_review(
                summary,
                year=req.year or now.year,
                generated_at=now,
                reader_name=req.reader_name,
            )
        return build_highlights_digest(summary, generated_at=now, reader_name=req.reader_name)

    async def _build_operator(self, session: Any, req: ReportRequest, now: datetime) -> Report:
        src = OperatorSource(session)
        book_title = await self._book_title(session, req.book_id)
        ceiling = float(req.params.get("ceiling_seconds", self._ceiling))
        if req.kind is ReportKind.BUDGET:
            budget_snap = await src.budget_snapshot(ceiling_seconds=ceiling, book_id=req.book_id)
            titles = await self._book_titles(session)
            return build_budget_report(
                budget_snap, generated_at=now, book_title=book_title, book_titles=titles
            )
        if req.kind is ReportKind.QUALITY:
            quality_snap = await src.quality_snapshot(book_id=req.book_id)
            baseline = None
            base_dict = req.params.get("baseline")
            if isinstance(base_dict, dict):
                baseline = _quality_from_params(base_dict)
            return build_quality_report(
                quality_snap, generated_at=now, book_title=book_title, baseline=baseline
            )
        if req.kind is ReportKind.RENDER_THROUGHPUT:
            tp_snap = await src.throughput_snapshot(book_id=req.book_id)
            return build_throughput_report(tp_snap, generated_at=now, book_title=book_title)
        # Library overview.
        lib = await src.library_snapshot()
        scenes = await src.scene_rows(req.book_id) if req.book_id else ()
        return build_library_overview_report(
            lib, generated_at=now, scenes=scenes, book_title=book_title
        )

    async def _book_title(self, session: Any, book_id: str | None) -> str | None:
        if not book_id:
            return None
        book = await BookRepo(session).get(book_id)
        return book.title if book else None

    async def _book_titles(self, session: Any) -> dict[str, str]:
        books = await BookRepo(session).list_all()
        return {b.id: b.title for b in books}

    # ----------------------------------------------------------------- render #

    def brand_for(self, kind: ReportKind) -> Brand:
        """Certificates get the light print brand; everything else the house brand."""
        if kind is ReportKind.COMPLETION_CERTIFICATE:
            return certificate_brand()
        return self._brand

    async def generate(self, session: Any, req: ReportRequest) -> GeneratedReport:
        """Build → render → (dedup) store → index, returning the artifact.

        Idempotent on content: identical rendered bytes reuse the existing stored
        object + a new index row pointing at it is *not* created — the prior
        ready row is returned with ``deduped=True``.
        """
        report = await self.build_report(session, req)
        brand = self.brand_for(req.kind)
        data = render(report, req.fmt, brand)
        digest = content_hash(data)
        fmt_enum = ReportFormatEnum(req.fmt.value)
        repo = ReportArtifactRepo(session)

        existing = await repo.find_dedup(content_hash=digest, fmt=fmt_enum)
        if existing is not None and existing.storage_key:
            url = self._store.signed_url(existing.storage_key, ttl=self._ttl)
            return GeneratedReport(artifact=existing, data=data, download_url=url, deduped=True)

        owner = req.user_id if req.kind in READER_KINDS else None
        try:
            key, _ = self._store.put(data, owner_id=owner, kind=req.kind.value, fmt=req.fmt)
        except Exception as exc:  # noqa: BLE001 - record the failure on the row
            logger.warning("report storage failed: %s", exc)
            row = await repo.create(
                kind=req.kind,
                audience=audience_for(req.kind),
                fmt=fmt_enum,
                title=report.meta.title,
                user_id=req.user_id,
                book_id=req.book_id,
                subject_kind=report.meta.kind,
                subject_id=report.meta.subject,
                status=ReportStatus.FAILED,
                trigger=req.trigger,
                params=dict(req.params) or None,
                error=str(exc),
            )
            raise ReportGenerationError(f"failed to store report: {exc}") from exc

        row = await repo.create(
            kind=req.kind,
            audience=audience_for(req.kind),
            fmt=fmt_enum,
            title=report.meta.title,
            user_id=req.user_id,
            book_id=req.book_id,
            subject_kind=report.meta.kind,
            subject_id=report.meta.subject,
            storage_key=key,
            content_hash=digest,
            size_bytes=len(data),
            status=ReportStatus.READY,
            trigger=req.trigger,
            params=dict(req.params) or None,
        )
        url = self._store.signed_url(key, ttl=self._ttl)
        return GeneratedReport(artifact=row, data=data, download_url=url, deduped=False)

    def signed_url(self, storage_key: str) -> str:
        """A signed retrieval URL for an already-stored artifact."""
        return self._store.signed_url(storage_key, ttl=self._ttl)


def _quality_from_params(d: dict[str, Any]) -> QualitySnapshot:
    """Reconstruct a baseline :class:`QualitySnapshot` from request params."""
    return QualitySnapshot(
        total_shots=int(d.get("total_shots", 0)),
        accepted_shots=int(d.get("accepted_shots", 0)),
        degraded_shots=int(d.get("degraded_shots", 0)),
        conflict_shots=int(d.get("conflict_shots", 0)),
        total_video_seconds=float(d.get("total_video_seconds", 0.0)),
        accepted_video_seconds=float(d.get("accepted_video_seconds", 0.0)),
        regen_count=int(d.get("regen_count", 0)),
        defect_count=int(d.get("defect_count", 0)),
        mean_ccs=d.get("mean_ccs"),
        mean_critic_score=d.get("mean_critic_score"),
    )


__all__ = [
    "GeneratedReport",
    "OPERATOR_KINDS",
    "READER_KINDS",
    "ReportGenerationError",
    "ReportRequest",
    "ReportService",
    "audience_for",
]
