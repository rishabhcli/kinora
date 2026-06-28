"""Reports API — generate, list, and retrieve report documents.

The HTTP surface over the reports subsystem (:mod:`app.reports`):

* ``POST /reports`` — generate a report on demand. The body picks a kind +
  format + scope; the response carries the artifact metadata and a signed
  download URL. Reader kinds are scoped to the caller; operator kinds require
  the caller be a configured report operator (``Settings.is_report_operator``).
* ``GET  /reports`` — list the caller's reader artifacts (newest first).
* ``GET  /reports/{id}`` — fetch one artifact's metadata + a fresh signed URL.
* ``GET  /reports/{id}/download`` — stream the artifact bytes inline (so the
  desktop shell can render a report without a public object URL).
* ``GET  /reports/preview`` — render a report's **model JSON** without storing
  it (the machine contract / a cheap preview).

Every operator endpoint scopes its data through the read-only sources; no route
mutates pipeline state or spends video-seconds.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app.api.deps import ContainerDep, CurrentUser
from app.api.errors import APIError
from app.composition import Container
from app.core.logging import get_logger
from app.db.models.user import User
from app.reports.db_model import ReportArtifact, ReportKind
from app.reports.render import ReportFormat, media_type
from app.reports.repository import ReportArtifactRepo
from app.reports.service import (
    READER_KINDS,
    GeneratedReport,
    ReportGenerationError,
    ReportRequest,
    ReportService,
)
from app.reports.storage import ReportArtifactStore

logger = get_logger("app.api.reports")

router = APIRouter(prefix="/reports", tags=["reports"])


# --------------------------------------------------------------------------- #
# Schemas
# --------------------------------------------------------------------------- #


class GenerateReportBody(BaseModel):
    """Request body for on-demand report generation."""

    kind: ReportKind
    format: ReportFormat = ReportFormat.PDF
    book_id: str | None = None
    year: int | None = None
    params: dict[str, Any] = Field(default_factory=dict)


class ArtifactResponse(BaseModel):
    """The metadata projection of a stored report artifact."""

    id: str
    kind: str
    audience: str
    format: str
    status: str
    title: str
    book_id: str | None
    subject_kind: str | None
    subject_id: str | None
    size_bytes: int | None
    content_hash: str | None
    trigger: str | None
    created_at: str
    download_url: str | None = None

    @staticmethod
    def of(row: ReportArtifact, *, download_url: str | None = None) -> ArtifactResponse:
        return ArtifactResponse(
            id=row.id,
            kind=str(row.kind.value),
            audience=str(row.audience.value),
            format=str(row.format.value),
            status=str(row.status.value),
            title=row.title,
            book_id=row.book_id,
            subject_kind=row.subject_kind,
            subject_id=row.subject_id,
            size_bytes=row.size_bytes,
            content_hash=row.content_hash,
            trigger=row.trigger,
            created_at=row.created_at.isoformat() if row.created_at else "",
            download_url=download_url,
        )


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _service(container: Container) -> ReportService:
    """Build a :class:`ReportService` wired to the container's object store."""
    store = ReportArtifactStore(container.object_store)
    return ReportService(
        artifact_store=store,
        url_ttl=getattr(container.settings, "report_url_ttl_s", 3600),
        ceiling_seconds=container.settings.budget_ceiling_video_s,
    )


def _authorize(container: Container, user: User, kind: ReportKind) -> None:
    """403 unless the caller may request this report kind."""
    if kind in READER_KINDS:
        return
    if not container.settings.is_report_operator(user.email):
        raise APIError(
            "forbidden",
            "operator reports require a report-operator account",
            status=403,
        )


async def _owned_book(container: Container, user: User, book_id: str) -> None:
    """404 unless ``book_id`` exists and is owned by ``user``."""
    from app.db.repositories.book import BookRepo

    async with container.session_factory() as session:
        book = await BookRepo(session).get(book_id)
    if book is None or book.user_id != user.id:
        raise APIError("book_not_found", "no such book for this user", status=404)


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #


@router.post("", response_model=ArtifactResponse, status_code=201)
async def generate_report(
    body: GenerateReportBody, container: ContainerDep, user: CurrentUser
) -> ArtifactResponse:
    """Generate a report on demand and return its metadata + download URL."""
    _authorize(container, user, body.kind)
    # Reader reports are always scoped to the caller's own book.
    if body.kind in READER_KINDS and body.book_id:
        await _owned_book(container, user, body.book_id)
    service = _service(container)
    req = ReportRequest(
        kind=body.kind,
        fmt=body.format,
        user_id=user.id if body.kind in READER_KINDS else None,
        book_id=body.book_id,
        reader_name=_display_name(user),
        year=body.year,
        trigger="on_demand",
        params=body.params,
    )
    try:
        async with container.session_factory() as session:
            result: GeneratedReport = await service.generate(session, req)
            await session.commit()
    except ReportGenerationError as exc:
        raise APIError("report_failed", str(exc), status=400) from exc
    return ArtifactResponse.of(result.artifact, download_url=result.download_url)


@router.get("", response_model=list[ArtifactResponse])
async def list_reports(
    container: ContainerDep,
    user: CurrentUser,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    kind: ReportKind | None = None,
) -> list[ArtifactResponse]:
    """List the caller's reader-facing report artifacts, newest first."""
    async with container.session_factory() as session:
        rows = await ReportArtifactRepo(session).list_for_user(user.id, limit=limit, kind=kind)
    return [ArtifactResponse.of(r) for r in rows]


@router.get("/preview")
async def preview_report(
    container: ContainerDep,
    user: CurrentUser,
    kind: ReportKind,
    book_id: str | None = None,
    year: int | None = None,
) -> dict[str, Any]:
    """Render a report's model JSON without storing it (the machine contract)."""
    _authorize(container, user, kind)
    if kind in READER_KINDS and book_id:
        await _owned_book(container, user, book_id)
    service = _service(container)
    req = ReportRequest(
        kind=kind,
        fmt=ReportFormat.JSON,
        user_id=user.id if kind in READER_KINDS else None,
        book_id=book_id,
        reader_name=_display_name(user),
        year=year,
        trigger="preview",
    )
    try:
        async with container.session_factory() as session:
            report = await service.build_report(session, req)
    except ReportGenerationError as exc:
        raise APIError("report_failed", str(exc), status=400) from exc
    return report.to_dict()


@router.get("/{artifact_id}", response_model=ArtifactResponse)
async def get_report(
    artifact_id: str, container: ContainerDep, user: CurrentUser
) -> ArtifactResponse:
    """Fetch one artifact's metadata + a fresh signed download URL."""
    row = await _load_owned(container, user, artifact_id)
    service = _service(container)
    url = service.signed_url(row.storage_key) if row.storage_key else None
    return ArtifactResponse.of(row, download_url=url)


@router.get("/{artifact_id}/download")
async def download_report(
    artifact_id: str, container: ContainerDep, user: CurrentUser
) -> Response:
    """Stream a stored artifact's bytes inline (no public object URL needed)."""
    row = await _load_owned(container, user, artifact_id)
    if not row.storage_key:
        raise APIError("report_not_ready", "this report has no stored artifact", status=409)
    store = ReportArtifactStore(container.object_store)
    try:
        data = store.fetch(row.storage_key)
    except Exception as exc:  # noqa: BLE001
        raise APIError(
            "report_unavailable", "stored artifact could not be read", status=502
        ) from exc
    fmt = ReportFormat(row.format.value)
    mt = media_type(fmt)
    filename = f"{row.kind.value}-{row.id[:8]}.{mt.extension}"
    return Response(
        content=data,
        media_type=mt.content_type,
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )


async def _load_owned(container: Container, user: User, artifact_id: str) -> ReportArtifact:
    """Load an artifact the caller may read (own reader report, or operator)."""
    async with container.session_factory() as session:
        row = await ReportArtifactRepo(session).get(artifact_id)
    if row is None:
        raise APIError("report_not_found", "no such report", status=404)
    is_owner = row.user_id == user.id
    is_operator = (
        str(row.audience.value) == "operator"
        and container.settings.is_report_operator(user.email)
    )
    if not (is_owner or is_operator):
        raise APIError("report_not_found", "no such report", status=404)
    return row


def _display_name(user: User) -> str:
    """A friendly reader name from the email local-part (no profile name yet)."""
    local = user.email.split("@", 1)[0]
    return local.replace(".", " ").replace("_", " ").title() or "Reader"


__all__ = ["router"]
