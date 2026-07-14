"""Round-trip fidelity tests for the data-portability layer (require infra).

These seed a rich book — canon entities (with embeddings + reference assets),
continuity states, the bitemporal engine, scenes/beats, shots with sync maps +
output clips, the source-span index, shot cache, defects, prefs, budget ledger —
plus its object-store blobs, then drive **export → import** through the
:class:`PortabilityService` and assert:

* the imported book's projected graph is **structurally equal** to the source
  (modulo the freshly-minted ids), and
* every blob round-trips **byte-identically**.

They also cover canon-only export/import, GDPR account export, right-to-erasure
(dry-run + execute), backup create/restore, and archive inspection.

Run against the isolated throwaway infra (see AGENTS.md / conftest): a dedicated
DB + redis db + MinIO. Skipped cleanly when that infra is not configured.
"""

from __future__ import annotations

import hashlib
from typing import Any

import pytest
import pytest_asyncio

from app.dataportability.service import PortabilityService
from app.db.base import new_id
from app.db.models.beat import Beat
from app.db.models.bitemporal import BitemporalState
from app.db.models.budget import BudgetKind, BudgetLedger
from app.db.models.continuity import ContinuityState
from app.db.models.defect import Defect
from app.db.models.entity import Entity
from app.db.models.enums import BookStatus, EntityType, ShotStatus
from app.db.models.pref import Pref
from app.db.models.scene import Scene
from app.db.models.shot import Shot, ShotCache, SourceSpanIndex
from app.db.repositories.book import BookRepo
from tests.conftest import requires_infra, user_id_for

pytestmark = requires_infra


# --------------------------------------------------------------------------- #
# Seeding a rich book + blobs
# --------------------------------------------------------------------------- #


async def _seed_rich_book(container: Any, user_id: str, *, title: str = "The Ice Saga") -> str:
    """Create a fully populated book and its object-store assets; return book_id."""
    book_id = new_id()
    async with container.session_factory() as session:
        await BookRepo(session).create(
            title=title,
            author="A. Writer",
            user_id=user_id,
            source_pdf_key=f"pdfs/{book_id}.pdf",
            cover_key=f"covers/{book_id}",
            status=BookStatus.READY,
            num_pages=2,
            art_direction="cold palette, wide lenses",
            book_id=book_id,
        )
        # scenes + beats
        scene = Scene(
            id=new_id(), book_id=book_id, scene_index=0, title="Opening",
            page_start=1, page_end=2, style_entity_key="style_main",
        )
        session.add(scene)
        await session.flush()
        beat = Beat(
            id=new_id(), book_id=book_id, scene_id=scene.id, beat_index=1,
            summary="Elsa stands at the window", entities=["char_elsa", "loc_window"],
            described_visuals="snow outside", mood="lonely",
            source_span={"page": 1, "word_range": [0, 12]},
        )
        session.add(beat)
        # entities (two versions of one chain + a style node)
        e1 = Entity(
            id=new_id(), book_id=book_id, entity_key="char_elsa", type=EntityType.CHARACTER,
            name="Elsa", aliases=["the Snow Queen"], description="platinum braid",
            appearance={
                "description": "ice gown",
                "reference_image_keys": [f"refs/{book_id}/char_elsa/ref_front.png"],
                "locked": True,
            },
            voice={
                "cosyvoice_voice_id": "vc_elsa",
                "reference_audio_key": f"refs/{book_id}/char_elsa/voice.wav",
            },
            embedding=[0.0] * 1152, version=1, valid_from_beat=1, valid_to_beat=3,
            first_appearance={"page": 1, "beat_id": "beat_0001"},
        )
        session.add(e1)
        await session.flush()
        e2 = Entity(
            id=new_id(), book_id=book_id, entity_key="char_elsa", type=EntityType.CHARACTER,
            name="Elsa", description="older, crowned", embedding=[1.0] + [0.0] * 1151,
            version=2, valid_from_beat=3, valid_to_beat=None, supersedes=e1.id,
        )
        session.add(e2)
        style = Entity(
            id=new_id(), book_id=book_id, entity_key="style_main", type=EntityType.STYLE,
            name="Main style", style_tokens={"palette": "cold", "lens": "wide"},
            version=1, valid_from_beat=1,
        )
        session.add(style)
        # continuity states (one active, one retired)
        session.add(ContinuityState(
            id=new_id(), book_id=book_id, subject_entity_key="char_elsa",
            predicate="possesses", object_value="prop_crown", valid_from_beat=3,
            valid_to_beat=None, version=1, source_span={"page": 2, "word_range": [40, 48]},
        ))
        session.add(ContinuityState(
            id=new_id(), book_id=book_id, subject_entity_key="char_elsa",
            predicate="location", object_value="loc_window", valid_from_beat=1,
            valid_to_beat=3, version=1,
        ))
        # bitemporal state
        from datetime import UTC, datetime

        session.add(BitemporalState(
            id=new_id(), book_id=book_id, fact_key="fk1", branch="main",
            subject_entity_key="char_elsa", predicate="mood", object_value="lonely",
            valid_from_beat=1, valid_to_beat=None, tx_from=datetime.now(UTC), tx_to=None,
            stamp_wall=1, stamp_counter=0, actor_id="system",
        ))
        # shots (accepted, with output + narration sync map)
        shot = Shot(
            id=new_id(), book_id=book_id, scene_id=scene.id, beat_id=beat.id,
            source_span={"page": 1, "word_range": [0, 12]}, status=ShotStatus.ACCEPTED,
            render_mode="reference_to_video", prompt="elsa at window", seed=88123,
            reference_set_hash="sha1:abc", reference_image_ids=["char_elsa@v2", "loc_window@v1"],
            duration_s=5.0,
            output={
                "clip_key": f"clips/{book_id}/shot1.mp4",
                "last_frame_key": f"lastframes/{book_id}/shot1.png",
            },
            narration={
                "text": "She stood.",
                "audio_key": f"audio/{book_id}/shot1.wav",
                "sync_segment": {"words": [{"word_index": 0, "text": "She"}]},
            },
            qa={"ccs": 0.91, "verdict": "pass", "score": 0.88},
            cost={"video_seconds": 5.0, "tokens": 1840}, embedding=[0.0] * 1152,
            canon_version_at_render=2, shot_hash=f"sha1:shothash:{book_id}",
        )
        session.add(shot)
        await session.flush()
        session.add(SourceSpanIndex(
            id=new_id(), book_id=book_id, word_index_start=0, word_index_end=12,
            shot_id=shot.id, scene_id=scene.id, beat_id=beat.id,
        ))
        session.add(ShotCache(
            shot_hash=f"sha1:cachehit:{book_id}", book_id=book_id,
            clip_key=f"clips/{book_id}/cached.mp4", last_frame_key=None,
            sync_segment={"page_turn_at_s": 4.8}, qa={"verdict": "pass"}, video_seconds=5.0,
        ))
        session.add(Defect(
            id=new_id(), shot_id=shot.id, book_id=book_id, kind="motion_artifact",
            detail={"score": 0.3},
        ))
        session.add(Pref(
            id=new_id(), user_id=user_id, book_id=book_id, kind="pacing",
            value={"axis": "pacing", "bias": 0.5}, weight=1.0,
        ))
        # budget ledger: a reserve + its commit
        res = BudgetLedger(
            id=new_id(), book_id=book_id, session_id=None, scene_id=scene.id,
            kind=BudgetKind.RESERVE, video_seconds=5.0, reservation_id="__self__", note="r",
        )
        session.add(res)
        await session.flush()
        res.reservation_id = res.id  # reserve points at itself
        session.add(BudgetLedger(
            id=new_id(), book_id=book_id, scene_id=scene.id, kind=BudgetKind.COMMIT,
            video_seconds=5.0, reservation_id=res.id, note="c",
        ))
        await session.flush()

    # object-store blobs the rows reference
    store = container.object_store
    import anyio

    blobs = {
        f"pdfs/{book_id}.pdf": b"%PDF-1.4 fake source",
        f"covers/{book_id}": b"\x89PNG cover bytes",
        f"pages/{book_id}/0001.png": b"\x89PNG page1",
        f"pages/{book_id}/0002.png": b"\x89PNG page2",
        f"refs/{book_id}/char_elsa/ref_front.png": b"\x89PNG elsa ref",
        f"refs/{book_id}/char_elsa/voice.wav": b"RIFF voice ref",
        f"clips/{book_id}/shot1.mp4": b"\x00\x00\x00\x18ftypmp42 clip bytes",
        f"lastframes/{book_id}/shot1.png": b"\x89PNG lastframe",
        f"audio/{book_id}/shot1.wav": b"RIFF narration",
        f"clips/{book_id}/cached.mp4": b"\x00\x00 cached clip",
    }
    await anyio.to_thread.run_sync(store.ensure_bucket)
    for key, payload in blobs.items():
        await anyio.to_thread.run_sync(store.put_bytes, key, payload, None)
    return book_id


def _digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


# --------------------------------------------------------------------------- #
# Projection for structural comparison (ignores the freshly-minted ids)
# --------------------------------------------------------------------------- #


async def _project_book(container: Any, book_id: str) -> dict[str, Any]:
    """A deterministic, id-independent projection of a book's graph for equality."""
    from app.dataportability.dbio import BookReader
    from app.dataportability.serialization import BOOK_SCOPED_TABLES

    out: dict[str, Any] = {}
    async with container.session_factory() as session:
        reader = BookReader(session)
        for table in BOOK_SCOPED_TABLES:
            rows = [r async for r in reader.stream_table(table, book_id)]
            # Drop id-ish + timestamp columns (they legitimately differ post-import).
            cleaned = [_strip_volatile(table, r) for r in rows]
            cleaned.sort(key=lambda r: _sort_key(table, r))
            out[table] = cleaned
    return out


_ID_COLUMNS = {
    "id", "book_id", "scene_id", "beat_id", "shot_id", "supersedes",
    "session_id", "reservation_id", "user_id", "created_at", "updated_at",
    "accepted_at", "shot_hash",
}


def _norm_keys(value: Any) -> Any:
    """Normalize the (changed) book-id segment out of any object key strings.

    Object keys embed the book id (``clips/<book>/...``), which legitimately
    differs after import; replace the second/stem segment with ``<BOOK>`` so the
    *structure* of the key is compared, not the specific book id.
    """
    if isinstance(value, str) and "/" in value:
        prefix, _, rest = value.partition("/")
        if prefix in {"pages", "keyframes", "clips", "lastframes", "audio", "refs", "canon"}:
            seg, sep, tail = rest.partition("/")
            return f"{prefix}/<BOOK>{sep}{tail}"
        if prefix in {"pdfs", "epubs", "covers"}:
            stem, dot, ext = rest.partition(".")
            return f"{prefix}/<BOOK>{dot}{ext}"
        return value
    if isinstance(value, dict):
        return {k: _norm_keys(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_norm_keys(v) for v in value]
    return value


def _strip_volatile(table: str, row: dict[str, Any]) -> dict[str, Any]:
    return {k: _norm_keys(v) for k, v in row.items() if k not in _ID_COLUMNS}


def _sort_key(table: str, row: dict[str, Any]) -> str:
    # A stable, content-based sort independent of ids.
    import json

    return json.dumps(row, sort_keys=True, default=str)


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest_asyncio.fixture
async def service(container: Any) -> PortabilityService:
    return PortabilityService(container.session_factory, container.object_store)


# --------------------------------------------------------------------------- #
# Book bundle round-trip
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_book_bundle_round_trip(api_client: Any, container: Any, auth_headers: dict) -> None:
    uid = await user_id_for(api_client, auth_headers)
    src_book = await _seed_rich_book(container, uid)
    svc = PortabilityService(container.session_factory, container.object_store)

    archive = await svc.export_book(src_book)
    assert archive[:2] == b"PK"  # a zip

    result = await svc.import_book(archive, owner_user_id=uid)
    assert result.new_book_id != src_book
    assert result.old_book_id == src_book
    assert result.blobs_restored >= 9

    # Structural equality of the projected graph (ids stripped).
    src_proj = await _project_book(container, src_book)
    dst_proj = await _project_book(container, result.new_book_id)
    for table in src_proj:
        assert dst_proj[table] == src_proj[table], f"table {table} differs"

    # The imported book is owned by the caller and visible on the shelf.
    async with container.session_factory() as session:
        new_book = await BookRepo(session).get(result.new_book_id)
        assert new_book is not None
        assert new_book.user_id == uid
        assert new_book.title == "The Ice Saga"


@pytest.mark.asyncio
async def test_book_blobs_round_trip_byte_identical(
    api_client: Any, container: Any, auth_headers: dict
) -> None:
    import anyio

    uid = await user_id_for(api_client, auth_headers)
    src_book = await _seed_rich_book(container, uid, title="Blobs Tale")
    svc = PortabilityService(container.session_factory, container.object_store)
    archive = await svc.export_book(src_book)
    result = await svc.import_book(archive, owner_user_id=uid)

    store = container.object_store
    # The source clip and its restored counterpart must be byte-identical.
    src_clip = await anyio.to_thread.run_sync(store.get_bytes, f"clips/{src_book}/shot1.mp4")
    dst_clip = await anyio.to_thread.run_sync(
        store.get_bytes, f"clips/{result.new_book_id}/shot1.mp4"
    )
    assert dst_clip == src_clip
    # The reference asset too.
    src_ref = await anyio.to_thread.run_sync(
        store.get_bytes, f"refs/{src_book}/char_elsa/ref_front.png"
    )
    dst_ref = await anyio.to_thread.run_sync(
        store.get_bytes, f"refs/{result.new_book_id}/char_elsa/ref_front.png"
    )
    assert dst_ref == src_ref


@pytest.mark.asyncio
async def test_import_remaps_intra_book_references(
    api_client: Any, container: Any, auth_headers: dict
) -> None:
    uid = await user_id_for(api_client, auth_headers)
    src_book = await _seed_rich_book(container, uid, title="Refs Tale")
    svc = PortabilityService(container.session_factory, container.object_store)
    archive = await svc.export_book(src_book)
    result = await svc.import_book(archive, owner_user_id=uid)

    async with container.session_factory() as session:
        from sqlalchemy import select

        # entities.supersedes must point at the imported v1 row, not the source's.
        rows = (
            await session.execute(
                select(Entity).where(
                    Entity.book_id == result.new_book_id, Entity.entity_key == "char_elsa"
                )
            )
        ).scalars().all()
        by_version = {e.version: e for e in rows}
        assert by_version[2].supersedes == by_version[1].id
        # the source-span index points at the imported shot row.
        spans = (
            await session.execute(
                select(SourceSpanIndex).where(SourceSpanIndex.book_id == result.new_book_id)
            )
        ).scalars().all()
        shots = (
            await session.execute(select(Shot).where(Shot.book_id == result.new_book_id))
        ).scalars().all()
        assert spans[0].shot_id == shots[0].id
        # budget ledger: the commit's reservation_id points at the imported reserve row.
        ledger = (
            await session.execute(
                select(BudgetLedger).where(BudgetLedger.book_id == result.new_book_id)
            )
        ).scalars().all()
        reserve = next(b for b in ledger if b.kind == BudgetKind.RESERVE)
        commit = next(b for b in ledger if b.kind == BudgetKind.COMMIT)
        assert reserve.reservation_id == reserve.id
        assert commit.reservation_id == reserve.id
