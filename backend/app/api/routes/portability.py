"""Data portability API — export/import book bundles, canon, accounts, backups.

Owner-scoped routes over :class:`app.dataportability.service.PortabilityService`:

* ``GET  /books/{id}/export``        — stream a full ``.kinora`` book bundle.
* ``POST /books/import``             — upload a bundle → new owned book (id-remapped).
* ``GET  /books/{id}/canon/export``  — stream a canon-only archive.
* ``POST /books/{id}/canon/import``  — merge/replace a canon archive into a book.
* ``GET  /me/export``                — stream a full GDPR account archive.
* ``POST /me/erasure``               — right-to-erasure (``?dry_run=true`` → plan).
* ``POST /backups``                  — snapshot the caller's books (or a subset).
* ``GET  /backups``                  — list stored snapshots (newest-first).
* ``POST /backups/{snapshot_id}/restore`` — restore a snapshot under the caller.
* ``POST /archives/inspect``         — verify + summarize an uploaded archive.

Exports stream so a multi-hundred-MB bundle never fully buffers in the response
layer; imports read a (size-capped) upload. Ownership uses the durable
``books.user_id`` column, identical to ``books.py`` / ``films.py``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.api.deps import ContainerDep, CurrentUser, write_rate_limit
from app.api.errors import APIError
from app.composition import Container
from app.dataportability.account import AccountImportResult, ErasurePlan
from app.dataportability.backup import RestoreResult, SnapshotEntry
from app.dataportability.book_import import BookImportResult
from app.dataportability.canon_import import CanonImportResult
from app.dataportability.errors import (
    ArchiveFormatError,
    ArchiveKindMismatchError,
    ChecksumMismatchError,
    PortabilityError,
    ReferentialIntegrityError,
    UnsupportedArchiveVersionError,
)
from app.dataportability.service import ArchiveInspection, PortabilityService
from app.db.models.user import User
from app.db.repositories.book import BookRepo

router = APIRouter(tags=["portability"])

#: Max upload size for an imported archive (256 MiB). Bundles with large clips can
#: exceed this; the route streams the read and aborts past the cap so an oversized
#: body never fully buffers. Operators can raise it for big libraries.
MAX_ARCHIVE_BYTES = 256 * 1024 * 1024
_READ_CHUNK = 1 << 20


# --------------------------------------------------------------------------- #
# Request/response models
# --------------------------------------------------------------------------- #


class CanonImportRequest(BaseModel):
    """Form-equivalent body for canon import (mode)."""

    mode: str = "replace"


class CreateBackupRequest(BaseModel):
    """Snapshot request: optional explicit book set + label."""

    book_ids: list[str] | None = None
    label: str | None = None


class BackupListResponse(BaseModel):
    """The backup catalog projected for the API."""

    snapshots: list[SnapshotEntry] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _service(container: Container) -> PortabilityService:
    # ObjectStore satisfies the DeletableBlobStore protocol (it has delete()).
    return PortabilityService(container.session_factory, container.object_store)


async def _assert_owner(container: Container, user: User, book_id: str) -> None:
    async with container.session_factory() as session:
        book = await BookRepo(session).get(book_id)
    if book is None or book.user_id != user.id:
        raise APIError("book_not_found", "no such book for this user", status=404)


async def _read_capped(file: UploadFile, cap: int) -> bytes:
    """Read an upload in chunks, aborting once it exceeds ``cap`` bytes."""
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(_READ_CHUNK)
        if not chunk:
            break
        total += len(chunk)
        if total > cap:
            raise APIError(
                "file_too_large",
                "archive exceeds the size limit",
                status=413,
                detail={"max_bytes": cap},
            )
        chunks.append(chunk)
    return b"".join(chunks)


def _stream_bytes(data: bytes, filename: str) -> StreamingResponse:
    """Return ``data`` as a downloadable ``.kinora`` attachment (chunked)."""

    async def _iter() -> AsyncIterator[bytes]:
        for i in range(0, len(data), _READ_CHUNK):
            yield data[i : i + _READ_CHUNK]

    return StreamingResponse(
        _iter(),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Length": str(len(data)),
        },
    )


def _map_portability_error(exc: PortabilityError) -> APIError:
    """Map a portability failure to a typed gateway error."""
    if isinstance(exc, ChecksumMismatchError):
        return APIError("archive_corrupt", str(exc), status=422)
    if isinstance(exc, ArchiveFormatError):
        return APIError("archive_malformed", str(exc), status=400)
    if isinstance(exc, ArchiveKindMismatchError):
        return APIError("archive_wrong_kind", str(exc), status=400)
    if isinstance(exc, UnsupportedArchiveVersionError):
        return APIError("archive_unsupported_version", str(exc), status=400)
    if isinstance(exc, ReferentialIntegrityError):
        return APIError("archive_integrity", str(exc), status=422)
    return APIError("portability_error", str(exc), status=400)


# --------------------------------------------------------------------------- #
# Book bundle
# --------------------------------------------------------------------------- #


@router.get("/books/{book_id}/export")
async def export_book(
    book_id: str, container: ContainerDep, user: CurrentUser
) -> StreamingResponse:
    """Stream a full ``.kinora`` book bundle (rows + blobs) for download."""
    await _assert_owner(container, user, book_id)
    data = await _service(container).export_book(book_id)
    return _stream_bytes(data, f"kinora-book-{book_id}.kinora")


@router.post("/books/import", response_model=BookImportResult, status_code=status.HTTP_201_CREATED)
async def import_book(
    container: ContainerDep,
    user: CurrentUser,
    _rl: Annotated[None, Depends(write_rate_limit)],
    file: Annotated[UploadFile, File(description="A .kinora book bundle")],
) -> BookImportResult:
    """Import a book bundle as a new book owned by the caller (id-remapped)."""
    data = await _read_capped(file, MAX_ARCHIVE_BYTES)
    try:
        return await _service(container).import_book(data, owner_user_id=user.id)
    except PortabilityError as exc:
        raise _map_portability_error(exc) from exc


# --------------------------------------------------------------------------- #
# Canon graph
# --------------------------------------------------------------------------- #


@router.get("/books/{book_id}/canon/export")
async def export_canon(
    book_id: str, container: ContainerDep, user: CurrentUser
) -> StreamingResponse:
    """Stream a canon-only archive (the §8 bible) for download."""
    await _assert_owner(container, user, book_id)
    data = await _service(container).export_canon(book_id)
    return _stream_bytes(data, f"kinora-canon-{book_id}.kinora")


@router.post("/books/{book_id}/canon/import", response_model=CanonImportResult)
async def import_canon(
    book_id: str,
    container: ContainerDep,
    user: CurrentUser,
    _rl: Annotated[None, Depends(write_rate_limit)],
    file: Annotated[UploadFile, File(description="A .kinora canon archive")],
    mode: Annotated[str, Form()] = "replace",
) -> CanonImportResult:
    """Merge or replace a book's canon graph from an uploaded canon archive."""
    await _assert_owner(container, user, book_id)
    if mode not in {"replace", "merge"}:
        raise APIError("invalid_mode", "mode must be 'replace' or 'merge'", status=400)
    data = await _read_capped(file, MAX_ARCHIVE_BYTES)
    try:
        return await _service(container).import_canon(
            data, target_book_id=book_id, mode=mode
        )
    except PortabilityError as exc:
        raise _map_portability_error(exc) from exc


# --------------------------------------------------------------------------- #
# Account / GDPR
# --------------------------------------------------------------------------- #


@router.get("/me/export")
async def export_account(
    container: ContainerDep, user: CurrentUser
) -> StreamingResponse:
    """Stream a full GDPR account archive (every owned book + profile)."""
    data = await _service(container).export_account(user.id)
    return _stream_bytes(data, f"kinora-account-{user.id}.kinora")


@router.post("/me/erasure", response_model=ErasurePlan)
async def erase_account(
    container: ContainerDep,
    user: CurrentUser,
    _rl: Annotated[None, Depends(write_rate_limit)],
    dry_run: Annotated[bool, Query()] = True,
) -> ErasurePlan:
    """Right-to-erasure. ``dry_run=true`` (default) returns the plan only.

    A destructive erase requires ``dry_run=false`` explicitly, so a stray call
    never deletes an account.
    """
    svc = _service(container)
    if dry_run:
        return await svc.erasure_plan(user.id)
    return await svc.erase_account(user.id)


@router.post(
    "/me/import", response_model=AccountImportResult, status_code=status.HTTP_201_CREATED
)
async def import_account(
    container: ContainerDep,
    user: CurrentUser,
    _rl: Annotated[None, Depends(write_rate_limit)],
    file: Annotated[UploadFile, File(description="A .kinora account archive")],
) -> AccountImportResult:
    """Import an account archive: re-create every book under the caller."""
    data = await _read_capped(file, MAX_ARCHIVE_BYTES)
    try:
        return await _service(container).import_account(data, owner_user_id=user.id)
    except PortabilityError as exc:
        raise _map_portability_error(exc) from exc


# --------------------------------------------------------------------------- #
# Backup / restore
# --------------------------------------------------------------------------- #


@router.post("/backups", response_model=SnapshotEntry, status_code=status.HTTP_201_CREATED)
async def create_backup(
    body: CreateBackupRequest,
    container: ContainerDep,
    user: CurrentUser,
    _rl: Annotated[None, Depends(write_rate_limit)],
) -> SnapshotEntry:
    """Snapshot the caller's books (all owned, or the requested subset)."""
    book_ids = body.book_ids
    async with container.session_factory() as session:
        owned = {b.id for b in await BookRepo(session).list_for_user(user.id)}
    if book_ids is None:
        book_ids = sorted(owned)
    else:
        unknown = [b for b in book_ids if b not in owned]
        if unknown:
            raise APIError(
                "book_not_found", "some requested books are not owned", status=404,
                detail={"unknown": unknown},
            )
    return await _service(container).create_backup(book_ids, label=body.label)


@router.get("/backups", response_model=BackupListResponse)
async def list_backups(container: ContainerDep, user: CurrentUser) -> BackupListResponse:
    """List stored backup snapshots (newest-first).

    Snapshots are shown only if every book they hold is owned by the caller, so a
    user never learns of another tenant's snapshots.
    """
    catalog = await _service(container).list_backups()
    async with container.session_factory() as session:
        owned = {b.id for b in await BookRepo(session).list_for_user(user.id)}
    visible = [
        s for s in catalog.snapshots if s.book_ids and all(b in owned for b in s.book_ids)
    ]
    return BackupListResponse(snapshots=visible)


@router.post("/backups/{snapshot_id}/restore", response_model=RestoreResult)
async def restore_backup(
    snapshot_id: str,
    container: ContainerDep,
    user: CurrentUser,
    _rl: Annotated[None, Depends(write_rate_limit)],
) -> RestoreResult:
    """Restore a snapshot's books under the caller (id-remapped, never in place)."""
    svc = _service(container)
    catalog = await svc.list_backups()
    entry = catalog.find(snapshot_id)
    if entry is None:
        raise APIError("snapshot_not_found", "no such snapshot", status=404)
    async with container.session_factory() as session:
        owned = {b.id for b in await BookRepo(session).list_for_user(user.id)}
    if not entry.book_ids or not all(b in owned for b in entry.book_ids):
        raise APIError("snapshot_not_found", "no such snapshot", status=404)
    try:
        return await svc.restore_backup(snapshot_id, owner_user_id=user.id)
    except PortabilityError as exc:
        raise _map_portability_error(exc) from exc


# --------------------------------------------------------------------------- #
# Inspect
# --------------------------------------------------------------------------- #


@router.post("/archives/inspect", response_model=ArchiveInspection)
async def inspect_archive(
    container: ContainerDep,
    user: CurrentUser,
    _rl: Annotated[None, Depends(write_rate_limit)],
    file: Annotated[UploadFile, File(description="Any .kinora archive")],
) -> ArchiveInspection:
    """Verify + summarize an uploaded archive without importing it."""
    data = await _read_capped(file, MAX_ARCHIVE_BYTES)
    return PortabilityService.inspect_archive(data)


__all__ = ["router"]
