"""Integration tests for canon/account/backup + the HTTP route (require infra).

Builds on the rich-book seeder in ``test_dataportability_roundtrip`` to cover:

* canon-only export → import (replace + merge) into a target book;
* GDPR account export (multi-book) + import;
* right-to-erasure (dry-run plan + executed delete + blob purge);
* backup create → list → inspect → restore → prune;
* the owner-scoped HTTP routes end-to-end (export streams, import uploads,
  erasure dry-run, archive inspection, cross-tenant isolation).
"""

from __future__ import annotations

from typing import Any

import anyio
import pytest
from sqlalchemy import func, select

from app.dataportability.manifest import ArchiveKind
from app.dataportability.service import PortabilityService
from app.db.models.continuity import ContinuityState
from app.db.models.entity import Entity
from app.db.models.user import User
from app.db.repositories.book import BookRepo
from tests.conftest import register_login, requires_infra, seed_owned_book, user_id_for
from tests.test_dataportability_roundtrip import _seed_rich_book

pytestmark = requires_infra


def _svc(container: Any) -> PortabilityService:
    return PortabilityService(container.session_factory, container.object_store)


# --------------------------------------------------------------------------- #
# Canon-only export / import
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_canon_export_import_replace(
    api_client: Any, container: Any, auth_headers: dict
) -> None:
    uid = await user_id_for(api_client, auth_headers)
    src = await _seed_rich_book(container, uid, title="Canon Src")
    target = await seed_owned_book(api_client, container, auth_headers, title="Canon Target")
    svc = _svc(container)

    archive = await svc.export_canon(src)
    result = await svc.import_canon(archive, target_book_id=target, mode="replace")
    assert result.target_book_id == target
    assert result.source_book_id == src
    assert result.table_counts.get("entities", 0) == 3  # 2 elsa versions + style

    async with container.session_factory() as session:
        ents = (
            await session.execute(select(Entity).where(Entity.book_id == target))
        ).scalars().all()
        keys = {e.entity_key for e in ents}
        assert keys == {"char_elsa", "style_main"}
        states = (
            await session.execute(
                select(ContinuityState).where(ContinuityState.book_id == target)
            )
        ).scalars().all()
        assert len(states) == 2  # one active, one retired
        # supersedes chain re-homed within the target book
        elsa = sorted((e for e in ents if e.entity_key == "char_elsa"), key=lambda e: e.version)
        assert elsa[1].supersedes == elsa[0].id


@pytest.mark.asyncio
async def test_canon_import_replace_clears_prior(
    api_client: Any, container: Any, auth_headers: dict
) -> None:
    uid = await user_id_for(api_client, auth_headers)
    src = await _seed_rich_book(container, uid, title="Src2")
    # target already has its OWN rich canon
    target = await _seed_rich_book(container, uid, title="Target2")
    svc = _svc(container)
    archive = await svc.export_canon(src)
    await svc.import_canon(archive, target_book_id=target, mode="replace")
    async with container.session_factory() as session:
        n = (
            await session.execute(
                select(func.count()).select_from(Entity).where(Entity.book_id == target)
            )
        ).scalar_one()
        assert n == 3  # replaced, not doubled


@pytest.mark.asyncio
async def test_canon_import_merge_adds(
    api_client: Any, container: Any, auth_headers: dict
) -> None:
    # Merge grafts a source canon into a book that has no overlapping entity_keys
    # (a plain target). Merging onto an overlapping (entity_key, version) is a
    # documented caller responsibility — the unique constraint would reject it.
    uid = await user_id_for(api_client, auth_headers)
    src = await _seed_rich_book(container, uid, title="Src3")
    target = await seed_owned_book(api_client, container, auth_headers, title="Target3")
    svc = _svc(container)
    archive = await svc.export_canon(src)
    await svc.import_canon(archive, target_book_id=target, mode="merge")
    async with container.session_factory() as session:
        n = (
            await session.execute(
                select(func.count()).select_from(Entity).where(Entity.book_id == target)
            )
        ).scalar_one()
        assert n == 3  # the source's 3 entities grafted in


# --------------------------------------------------------------------------- #
# GDPR account export/import + erasure
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_account_export_import_multi_book(
    api_client: Any, container: Any, make_user: Any
) -> None:
    # Source user owns two books; export the account, import under a fresh user.
    src_headers = await make_user("gdpr-src@example.com")
    src_uid = await user_id_for(api_client, src_headers)
    b1 = await _seed_rich_book(container, src_uid, title="Book One")
    b2 = await _seed_rich_book(container, src_uid, title="Book Two")
    svc = _svc(container)

    archive = await svc.export_account(src_uid)
    dst_headers = await make_user("gdpr-dst@example.com")
    dst_uid = await user_id_for(api_client, dst_headers)
    result = await svc.import_account(archive, owner_user_id=dst_uid)
    assert len(result.book_ids) == 2

    async with container.session_factory() as session:
        owned = await BookRepo(session).list_for_user(dst_uid)
        titles = {b.title for b in owned}
        assert titles == {"Book One", "Book Two"}
    _ = (b1, b2)


@pytest.mark.asyncio
async def test_erasure_dry_run_then_execute(
    api_client: Any, container: Any, make_user: Any
) -> None:
    headers = await make_user("erase-me@example.com")
    uid = await user_id_for(api_client, headers)
    book = await _seed_rich_book(container, uid, title="Doomed")
    svc = _svc(container)

    # Dry-run: nothing deleted, but the plan lists the book + its blobs + counts.
    plan = await svc.erasure_plan(uid)
    assert book in plan.book_ids
    assert not plan.executed
    assert plan.row_counts.get("shots", 0) == 1
    assert any(k.startswith("clips/") for k in plan.blob_keys)
    async with container.session_factory() as session:
        assert await BookRepo(session).get(book) is not None  # still there

    # Execute: book gone (cascade), user gone, a sampled blob purged.
    store = container.object_store
    clip_key = f"clips/{book}/shot1.mp4"
    assert await anyio.to_thread.run_sync(store.exists, clip_key)
    done = await svc.erase_account(uid, purge_blobs=True)
    assert done.executed
    async with container.session_factory() as session:
        assert await BookRepo(session).get(book) is None
        assert await session.get(User, uid) is None
    assert not await anyio.to_thread.run_sync(store.exists, clip_key)


# --------------------------------------------------------------------------- #
# Backup + restore
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_backup_create_inspect_restore_prune(
    api_client: Any, container: Any, auth_headers: dict
) -> None:
    uid = await user_id_for(api_client, auth_headers)
    book = await _seed_rich_book(container, uid, title="Backup Me")
    svc = _svc(container)

    entry = await svc.create_backup([book], label="nightly")
    assert entry.book_ids == [book]
    assert entry.size_bytes > 0

    catalog = await svc.list_backups()
    assert catalog.find(entry.snapshot_id) is not None

    manifest = await svc.inspect_backup(entry.snapshot_id)
    assert manifest is not None
    assert manifest.kind == ArchiveKind.BACKUP

    restored = await svc.restore_backup(entry.snapshot_id, owner_user_id=uid)
    assert len(restored.restored_book_ids) == 1
    new_book = restored.restored_book_ids[0]
    assert new_book != book
    async with container.session_factory() as session:
        rb = await BookRepo(session).get(new_book)
        assert rb is not None and rb.title == "Backup Me" and rb.user_id == uid

    # Make a second snapshot, prune to keep the newest 1.
    entry2 = await svc.create_backup([book], label="second")
    removed = await svc.prune_backups(keep_last=1)
    assert entry.snapshot_id in removed
    remaining = await svc.list_backups()
    ids = {s.snapshot_id for s in remaining.snapshots}
    assert entry2.snapshot_id in ids
    assert entry.snapshot_id not in ids


# --------------------------------------------------------------------------- #
# HTTP route
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_route_export_then_import_book(
    api_client: Any, container: Any, auth_headers: dict
) -> None:
    uid = await user_id_for(api_client, auth_headers)
    book = await _seed_rich_book(container, uid, title="Routed")

    resp = await api_client.get(f"/api/books/{book}/export", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == "application/zip"
    data = resp.content
    assert data[:2] == b"PK"

    files = {"file": ("book.kinora", data, "application/zip")}
    imp = await api_client.post("/api/books/import", headers=auth_headers, files=files)
    assert imp.status_code == 201, imp.text
    body = imp.json()
    assert body["new_book_id"] != book
    assert body["old_book_id"] == book

    # The new book is on the caller's shelf.
    shelf = await api_client.get("/api/books", headers=auth_headers)
    ids = {b["id"] for b in shelf.json()}
    assert body["new_book_id"] in ids


@pytest.mark.asyncio
async def test_route_export_forbidden_for_non_owner(
    api_client: Any, container: Any, auth_headers: dict, make_user: Any
) -> None:
    owner_uid = await user_id_for(api_client, auth_headers)
    book = await _seed_rich_book(container, owner_uid, title="Private")
    other = await make_user("intruder@example.com")
    resp = await api_client.get(f"/api/books/{book}/export", headers=other)
    assert resp.status_code == 404  # owner-scoped, fail-closed


@pytest.mark.asyncio
async def test_route_inspect_archive(
    api_client: Any, container: Any, auth_headers: dict
) -> None:
    uid = await user_id_for(api_client, auth_headers)
    book = await _seed_rich_book(container, uid, title="Inspect Me")
    export = await api_client.get(f"/api/books/{book}/export", headers=auth_headers)
    data = export.content

    files = {"file": ("any.kinora", data, "application/zip")}
    resp = await api_client.post("/api/archives/inspect", headers=auth_headers, files=files)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["verified"] is True
    assert body["kind"] == ArchiveKind.BOOK
    assert "shots" in body["tables"]

    # A corrupted upload reports verified=false, never raises.
    corrupt = bytearray(data)
    corrupt[100:120] = b"X" * 20
    files = {"file": ("bad.kinora", bytes(corrupt), "application/zip")}
    resp = await api_client.post("/api/archives/inspect", headers=auth_headers, files=files)
    assert resp.status_code == 200
    assert resp.json()["verified"] is False


@pytest.mark.asyncio
async def test_route_erasure_dry_run_default(
    api_client: Any, container: Any, make_user: Any
) -> None:
    headers = await make_user("route-erase@example.com")
    uid = await user_id_for(api_client, headers)
    book = await _seed_rich_book(container, uid, title="Keep For Now")

    # Default is dry-run; the book survives.
    resp = await api_client.post("/api/me/erasure", headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["executed"] is False
    async with container.session_factory() as session:
        assert await BookRepo(session).get(book) is not None


@pytest.mark.asyncio
async def test_route_canon_import_bad_mode(
    api_client: Any, container: Any, auth_headers: dict
) -> None:
    uid = await user_id_for(api_client, auth_headers)
    book = await _seed_rich_book(container, uid, title="Mode Test")
    export = await api_client.get(f"/api/books/{book}/canon/export", headers=auth_headers)
    files = {"file": ("canon.kinora", export.content, "application/zip")}
    resp = await api_client.post(
        f"/api/books/{book}/canon/import?mode=bogus",
        headers=auth_headers,
        files=files,
        data={"mode": "bogus"},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["type"] == "invalid_mode"


@pytest.mark.asyncio
async def test_route_import_wrong_kind_rejected(
    api_client: Any, container: Any, auth_headers: dict
) -> None:
    uid = await user_id_for(api_client, auth_headers)
    book = await _seed_rich_book(container, uid, title="Wrong Kind")
    # A canon archive pushed at the book-bundle import endpoint is rejected.
    canon = await api_client.get(f"/api/books/{book}/canon/export", headers=auth_headers)
    files = {"file": ("canon.kinora", canon.content, "application/zip")}
    resp = await api_client.post("/api/books/import", headers=auth_headers, files=files)
    assert resp.status_code == 400
    assert resp.json()["error"]["type"] == "archive_wrong_kind"


@pytest.mark.asyncio
async def test_route_backups_owner_scoped(
    api_client: Any, container: Any, auth_headers: dict, make_user: Any
) -> None:
    uid = await user_id_for(api_client, auth_headers)
    book = await _seed_rich_book(container, uid, title="Mine")
    create = await api_client.post(
        "/api/backups", headers=auth_headers, json={"book_ids": [book], "label": "x"}
    )
    assert create.status_code == 201, create.text
    snapshot_id = create.json()["snapshot_id"]

    # Owner sees it; an intruder does not.
    mine = await api_client.get("/api/backups", headers=auth_headers)
    assert any(s["snapshot_id"] == snapshot_id for s in mine.json()["snapshots"])
    intruder = await make_user("backup-intruder@example.com")
    theirs = await api_client.get("/api/backups", headers=intruder)
    assert all(s["snapshot_id"] != snapshot_id for s in theirs.json()["snapshots"])
    # Intruder cannot restore it.
    bad = await api_client.post(
        f"/api/backups/{snapshot_id}/restore", headers=intruder
    )
    assert bad.status_code == 404


_ = register_login  # re-exported for symmetry with the roundtrip module
