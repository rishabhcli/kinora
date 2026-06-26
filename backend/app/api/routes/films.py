"""Film API — the §9.6 *stitch + ship* boundary over HTTP (Agent 03).

Two read routes project a book's stitched **event/scene films** + sync maps for
the client (the contract is published in ``coordination/CONTRACTS.md`` and the
wire models live in :mod:`app.films.contract`):

* ``GET /api/books/{book_id}/events`` — every event (== scene today) with its
  stitched-film URL + merged sync map, plus the open-book ``restore`` state so
  Agent 12 can reopen a book where the reader left off (§5.2).
* ``GET /api/books/{book_id}/scenes/{scene_id}/film`` — one scene's film, for a
  partial load.

The sync map is built **on read** from the scene's accepted shots (§9.6), so the
endpoints work with ``KINORA_LIVE_VIDEO`` off and never block on rendering — an
unstitched film simply reports ``stitched: false`` with a ``null`` URL.

This module is registered by Agent 12 (see ``coordination/requests/agent-03.md``).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import anyio
from fastapi import APIRouter
from sqlalchemy import nullslast, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import ContainerDep, CurrentUser
from app.api.errors import APIError
from app.composition import Container
from app.db.models.beat import Beat
from app.db.models.enums import ShotStatus
from app.db.models.scene import Scene
from app.db.models.session import Session as SessionModel
from app.db.models.shot import Shot
from app.db.models.user import User
from app.db.repositories.book import BookRepo
from app.db.repositories.scene import SceneRepo
from app.db.repositories.shot import SourceSpanRepo
from app.films.contract import (
    EventFilm,
    EventsResponse,
    FilmSyncMap,
    RestoreState,
    SceneFilm,
    SceneRef,
    merge_and_build_film_sync_map,
)
from app.storage.object_store import keys

router = APIRouter(prefix="/books", tags=["films"])

#: Presigned-URL lifetime (seconds). Matches ``ObjectStore.presigned_get_url`` default.
URL_TTL_S = 3600


# --------------------------------------------------------------------------- #
# Pure projection helpers (ORM row -> contract shapes)
# --------------------------------------------------------------------------- #


def _span_range(shot: Shot) -> tuple[int, int] | None:
    raw = (shot.source_span or {}).get("word_range")
    if isinstance(raw, (list, tuple)) and len(raw) == 2:
        return (int(raw[0]), int(raw[1]))
    return None


def _span_start(shot: Shot) -> int:
    rng = _span_range(shot)
    return rng[0] if rng else 0


def _scene_word_range(shots: list[Shot]) -> tuple[int, int]:
    """The source-word span covered by a scene's shots (min start .. max end)."""
    ranges = [r for r in (_span_range(s) for s in shots) if r is not None]
    if not ranges:
        return (0, 0)
    return (min(r[0] for r in ranges), max(r[1] for r in ranges))


def _shot_segment(shot: Shot) -> dict[str, Any]:
    """The shot's per-shot (0-based) §9.4 sync segment, or a synthesized stand-in."""
    seg = (shot.narration or {}).get("sync_segment")
    if isinstance(seg, Mapping):
        return dict(seg)
    span = shot.source_span or {}
    dur = float(shot.duration_s or 0.0)
    return {
        "shot_id": shot.id,
        "video_start_s": 0.0,
        "video_end_s": round(dur, 3),
        "page": int(span.get("page", 0) or 0),
        "page_turn_at_s": round(max(0.0, dur - 0.2), 3),
        "words": [],
    }


def _film_sync_map(scene_id: str, shots: list[Shot]) -> FilmSyncMap:
    segments = [_shot_segment(s) for s in shots]
    spans = {s.id: (_span_range(s) or (0, 0)) for s in shots}
    return merge_and_build_film_sync_map(segments, scene_id=scene_id, spans=spans)


@dataclass(frozen=True)
class _SceneBlob:
    """A scene projected to plain data (built inside the DB session, used outside)."""

    scene_id: str
    scene_index: int
    page_start: int
    page_end: int
    word_range: tuple[int, int]
    shot_count: int
    sync_map: FilmSyncMap


def _scene_blob(scene: Scene, shots: list[Shot]) -> _SceneBlob:
    return _SceneBlob(
        scene_id=scene.id,
        scene_index=scene.scene_index,
        page_start=scene.page_start,
        page_end=scene.page_end,
        word_range=_scene_word_range(shots),
        shot_count=len(shots),
        sync_map=_film_sync_map(scene.id, shots),
    )


# --------------------------------------------------------------------------- #
# DB access
# --------------------------------------------------------------------------- #


async def _assert_owner(session: AsyncSession, user: User, book_id: str) -> None:
    """404 unless the book exists and is owned by the caller (mirrors books.py)."""
    book = await BookRepo(session).get(book_id)
    if book is None or book.user_id != user.id:
        raise APIError("book_not_found", "no such book", status=404)


async def _accepted_shots_by_scene(session: AsyncSession, book_id: str) -> dict[str, list[Shot]]:
    """A book's ACCEPTED shots grouped by scene, each in narrative order (§9.6).

    Ordered by ``Beat.beat_index`` to match the stitcher exactly
    (``SceneStitcher._accepted_shots_in_order``) — the cumulative film timeline
    is built in this order, so it must agree with the concatenated mp4. A LEFT
    join keeps beat-less accepted shots in the view (the stitcher inner-joins);
    they sort last, by ``word_range`` start, as a deterministic fallback.
    """
    stmt = (
        select(Shot, Beat.beat_index)
        .join(Beat, Beat.id == Shot.beat_id, isouter=True)
        .where(Shot.book_id == book_id, Shot.status == ShotStatus.ACCEPTED)
    )
    rows = list((await session.execute(stmt)).all())
    by_scene: dict[str, list[tuple[Shot, int | None]]] = {}
    for shot, beat_index in rows:
        if shot.scene_id:
            by_scene.setdefault(shot.scene_id, []).append((shot, beat_index))
    ordered: dict[str, list[Shot]] = {}
    for scene_id, pairs in by_scene.items():
        pairs.sort(
            key=lambda p: (p[1] is None, p[1] if p[1] is not None else 0, _span_start(p[0]))
        )
        ordered[scene_id] = [shot for shot, _ in pairs]
    return ordered


async def _restore_state(
    session: AsyncSession, book_id: str, user_id: str
) -> RestoreState | None:
    """Open-book context from the caller's most-recent session for this book (§5.2)."""
    stmt = (
        select(SessionModel)
        .where(SessionModel.book_id == book_id, SessionModel.user_id == user_id)
        .order_by(nullslast(SessionModel.last_activity_ms.desc()), SessionModel.updated_at.desc())
        .limit(1)
    )
    row = (await session.execute(stmt)).scalars().first()
    if row is None:
        return None
    current_index: int | None = None
    current_scene: str | None = None
    # Nearest-preceding-shot semantics: resolve_word_to_shot picks the greatest
    # word_index_start <= focus_word (it does not bound on word_index_end), so a
    # focus word in a trailing gap resolves to the last scene the reader reached —
    # the right "reopen where they left off" anchor. None only when before shot 1.
    shot = await SourceSpanRepo(session).resolve_word_to_shot(book_id, row.focus_word)
    if shot is not None and shot.scene_id:
        scene = await SceneRepo(session).get(shot.scene_id)
        if scene is not None:
            current_index = scene.scene_index
            current_scene = scene.id
    return RestoreState(
        session_id=row.id,
        focus_word=row.focus_word,
        current_event_index=current_index,
        current_scene_id=current_scene,
        mode=row.mode.value,
    )


# --------------------------------------------------------------------------- #
# Object-store URL projection (presign + refresh metadata)
# --------------------------------------------------------------------------- #


def _presign(container: Container, key: str, ttl: int) -> tuple[str, str | None]:
    """Presigned GET URL + ISO expiry (``None`` when the URL is a stable public URL)."""
    url = container.object_store.presigned_get_url(key, ttl=ttl)
    public = container.object_store.public_url(key)
    if public is not None and url == public:
        return url, None
    return url, (datetime.now(UTC) + timedelta(seconds=ttl)).isoformat()


async def _film_fields(
    container: Container, book_id: str, scene_id: str, ttl: int
) -> tuple[bool, str | None, str | None]:
    """``(stitched, oss_url, url_expires_at)`` for a scene's stitched mp4."""
    # The stitched-scene mp4 reuses keys.clip with the scene_id (matches
    # SceneStitcher in render/stitch.py); per-shot clips use it with a shot_id.
    # The id prefixes differ, so the namespaces never collide.
    clip_key = keys.clip(book_id, scene_id)
    stitched = await anyio.to_thread.run_sync(container.object_store.exists, clip_key)
    if not stitched:
        return False, None, None
    url, expires = _presign(container, clip_key, ttl)
    return True, url, expires


async def _assemble_event(
    container: Container, book_id: str, blob: _SceneBlob, ttl: int
) -> EventFilm:
    stitched, oss_url, expires = await _film_fields(container, book_id, blob.scene_id, ttl)
    duration = blob.sync_map.duration_s if blob.shot_count else None
    return EventFilm(
        event_id=blob.scene_id,
        event_index=blob.scene_index,
        book_id=book_id,
        page_start=blob.page_start,
        page_end=blob.page_end,
        word_range=blob.word_range,
        stitched=stitched,
        oss_url=oss_url,
        url_expires_at=expires,
        duration_s=duration,
        shot_count=blob.shot_count,
        sync_map=blob.sync_map,
        scenes=[
            SceneRef(
                scene_id=blob.scene_id,
                scene_index=blob.scene_index,
                word_range=blob.word_range,
                stitched=stitched,
                duration_s=duration,
            )
        ],
    )


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #


@router.get("/{book_id}/events", response_model=EventsResponse)
async def list_events(
    book_id: str, container: ContainerDep, user: CurrentUser
) -> EventsResponse:
    """Every event (scene) for a book — stitched URL + sync map + restore state."""
    async with container.session_factory() as session:
        await _assert_owner(session, user, book_id)
        scenes = await SceneRepo(session).list_by_book(book_id)
        shots_by_scene = await _accepted_shots_by_scene(session, book_id)
        blobs = [_scene_blob(sc, shots_by_scene.get(sc.id, [])) for sc in scenes]
        restore = await _restore_state(session, book_id, user.id)
    events = [await _assemble_event(container, book_id, blob, URL_TTL_S) for blob in blobs]
    return EventsResponse(
        book_id=book_id, url_ttl_s=URL_TTL_S, events=events, restore=restore
    )


@router.get("/{book_id}/scenes/{scene_id}/film", response_model=SceneFilm)
async def get_scene_film(
    book_id: str, scene_id: str, container: ContainerDep, user: CurrentUser
) -> SceneFilm:
    """One scene's film (partial load)."""
    async with container.session_factory() as session:
        await _assert_owner(session, user, book_id)
        scene = await SceneRepo(session).get(scene_id)
        if scene is None or scene.book_id != book_id:
            raise APIError("scene_not_found", "no such scene", status=404)
        shots = (await _accepted_shots_by_scene(session, book_id)).get(scene_id, [])
        blob = _scene_blob(scene, shots)
    stitched, oss_url, expires = await _film_fields(container, book_id, scene_id, URL_TTL_S)
    duration = blob.sync_map.duration_s if blob.shot_count else None
    return SceneFilm(
        scene_id=blob.scene_id,
        event_id=blob.scene_id,
        book_id=book_id,
        scene_index=blob.scene_index,
        event_index=blob.scene_index,
        page_start=blob.page_start,
        page_end=blob.page_end,
        word_range=blob.word_range,
        stitched=stitched,
        oss_url=oss_url,
        url_expires_at=expires,
        duration_s=duration,
        shot_count=blob.shot_count,
        sync_map=blob.sync_map,
    )


__all__ = ["router"]
