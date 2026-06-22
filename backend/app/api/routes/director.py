"""Director routes — region comments, surgical canon edits, conflict choices (§5.4).

* ``POST /sessions/{id}/comment`` classifies a region note with a cheap chat()
  call (Cinematographer pacing/look vs Continuity room/canon, §5.4), enqueues a
  targeted regen of the commented shot, and emits an ``agent_activity`` event.
* ``POST /books/{id}/canon_edit`` writes the entity edit via
  ``canon_service.upsert_entity``, then finds the shots whose reference set
  includes the changed entity, recomputes their ``shot_hash``, and enqueues a
  regen for **only those** (surgical re-render, §8.7), emitting ``regen_done`` as
  each completes — everything else still hits the cache.
* ``POST /sessions/{id}/conflict_choice`` records the Director's resolution of a
  surfaced conflict (§7.2).
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends
from sqlalchemy import select

from app.api.deps import ContainerDep, CurrentUser, write_rate_limit
from app.api.errors import APIError
from app.api.routes.sessions import _owned_session
from app.api.schemas import (
    CanonEditRequest,
    CanonEditResponse,
    CommentRequest,
    CommentResponse,
    ConflictChoiceRequest,
    ConflictChoiceResponse,
)
from app.composition import Container
from app.core.logging import get_logger
from app.db.hashing import compute_shot_hash
from app.db.models.enums import EntityType, ShotStatus
from app.db.models.shot import Shot
from app.db.repositories.book import BookRepo
from app.db.repositories.shot import ShotRepo
from app.memory.cache_service import CacheService
from app.memory.canon_service import CanonService
from app.memory.interfaces import ShotSpec
from app.queue.redis_queue import book_channel, session_channel

logger = get_logger("app.api.director")

router = APIRouter(tags=["director"])

# Golden-ratio seed step: a Director regen re-rolls to a genuinely new variation.
_SEED_STEP = 0x9E3779B1
_SEED_MASK = 0x7FFFFFFF


def _rotate_seed(seed: int | None) -> int:
    return (int(seed or 0) + _SEED_STEP) & _SEED_MASK


def _references_entity(reference_image_ids: list[str] | None, entity_key: str) -> bool:
    """Whether a shot's reference set includes the edited entity (any version)."""
    for ref in reference_image_ids or []:
        if ref == entity_key or ref.split("@", 1)[0] == entity_key:
            return True
    return False


def _spec_from_shot(shot: Shot, *, shot_hash: str, ref_hash: str) -> ShotSpec:
    return ShotSpec(
        book_id=shot.book_id,
        beat_id=shot.beat_id or "",
        scene_id=shot.scene_id,
        shot_id=shot.id,
        render_mode=shot.render_mode or "reference_to_video",
        seed=int(shot.seed or 0),
        reference_image_ids=list(shot.reference_image_ids or []),
        canon_version_at_render=int(shot.canon_version_at_render or 1),
        target_duration_s=float(shot.duration_s or 5.0),
        reference_set_hash=ref_hash,
        shot_hash=shot_hash,
    )


@router.post("/sessions/{session_id}/comment", response_model=CommentResponse)
async def comment(
    session_id: str,
    body: CommentRequest,
    container: ContainerDep,
    user: CurrentUser,
    _rl: Annotated[None, Depends(write_rate_limit)],
) -> CommentResponse:
    """Classify a Director region-comment, enqueue a regen, emit agent_activity (§5.4)."""
    row = await _owned_session(container, user, session_id)

    async with container.session_factory() as session:
        shot = await ShotRepo(session).get(body.shot_id)
    if shot is None or shot.book_id != row.book_id:
        raise APIError("shot_not_found", "no such shot in this session's book", status=404)

    shot_context = f"render_mode={shot.render_mode}; beat={shot.beat_id}; scene={shot.scene_id}"
    route = await container.classify_comment(body.note, shot_context=shot_context)

    # Re-roll the seed so the targeted regen is a real new variation (not a cache hit).
    new_seed = _rotate_seed(shot.seed)
    ref_hash = CacheService.reference_set_hash(list(shot.reference_image_ids or []))
    new_hash = compute_shot_hash(
        book_id=shot.book_id,
        beat_id=shot.beat_id or "",
        canon_version_at_render=int(shot.canon_version_at_render or 1),
        render_mode=shot.render_mode or "reference_to_video",
        seed=new_seed,
        reference_set_hash=ref_hash,
    )
    async with container.session_factory() as session:
        await ShotRepo(session).update(
            body.shot_id, seed=new_seed, shot_hash=new_hash,
            reference_set_hash=ref_hash, status=ShotStatus.PROMOTED,
        )

    spec = ShotSpec(
        book_id=shot.book_id,
        beat_id=shot.beat_id or "",
        scene_id=shot.scene_id,
        shot_id=shot.id,
        render_mode=shot.render_mode or "reference_to_video",
        seed=new_seed,
        reference_image_ids=list(shot.reference_image_ids or []),
        canon_version_at_render=int(shot.canon_version_at_render or 1),
        target_duration_s=float(shot.duration_s or 5.0),
        reference_set_hash=ref_hash,
        shot_hash=new_hash,
    )
    job_id = await container.enqueue_regen(spec)

    await container.redis.publish(
        session_channel(session_id),
        {
            "event": "agent_activity",
            "agent": route.agent,
            "aspect": route.aspect,
            "message": route.message,
            "shot_id": body.shot_id,
            "job_id": job_id or None,
        },
    )
    logger.info(
        "director.comment", session_id=session_id, shot_id=body.shot_id, agent=route.agent
    )
    return CommentResponse(
        shot_id=body.shot_id,
        agent=route.agent,
        aspect=route.aspect,
        message=route.message,
        job_id=job_id or None,
    )


@router.post("/books/{book_id}/canon_edit", response_model=CanonEditResponse)
async def canon_edit(
    book_id: str,
    body: CanonEditRequest,
    container: ContainerDep,
    user: CurrentUser,
    _rl: Annotated[None, Depends(write_rate_limit)],
) -> CanonEditResponse:
    """Edit a canon entity and surgically regen only the dependent shots (§8.7)."""
    # Durable ownership via books.user_id (fail-closed on a NULL owner).
    async with container.session_factory() as session:
        book = await BookRepo(session).get(book_id)
    if book is None or book.user_id != user.id:
        raise APIError("book_not_found", "no such book for this user", status=404)

    # 1. Apply the edit as a new entity version (Continuity Supervisor write, §8.1).
    async with container.session_factory() as session:
        canon = CanonService(
            session, embedder=container._embedder(), blob_store=container.object_store
        )
        current = await canon.get_entity(book_id, body.entity_key)
        if current is None:
            raise APIError("entity_not_found", "no such canon entity", status=404)
        changes: dict[str, Any] = dict(body.changes)
        version = await canon.upsert_entity(
            book_id=book_id,
            entity_key=body.entity_key,
            entity_type=EntityType(current.type),
            name=str(changes.get("name", current.name)),
            valid_from_beat=(
                body.valid_from_beat
                if body.valid_from_beat is not None
                else current.valid_from_beat
            ),
            aliases=changes.get("aliases", current.aliases),
            description=changes.get("description", current.description),
            appearance=changes.get("appearance", current.appearance),
            voice=changes.get("voice", current.voice),
            style_tokens=changes.get("style_tokens", current.style_tokens),
        )

    # 2. Find the dependent shots; recompute their shot_hash; enqueue ONLY those.
    regenerated: list[str] = []
    skipped = 0
    async with container.session_factory() as session:
        repo = ShotRepo(session)
        rows = list(
            (await session.execute(select(Shot).where(Shot.book_id == book_id)))
            .scalars()
            .all()
        )
        for shot in rows:
            if not _references_entity(shot.reference_image_ids, body.entity_key):
                skipped += 1
                continue
            ref_hash = CacheService.reference_set_hash(list(shot.reference_image_ids or []))
            new_hash = compute_shot_hash(
                book_id=book_id,
                beat_id=shot.beat_id or "",
                canon_version_at_render=version,
                render_mode=shot.render_mode or "reference_to_video",
                seed=int(shot.seed or 0),
                reference_set_hash=ref_hash,
            )
            await repo.update(
                shot.id,
                shot_hash=new_hash,
                reference_set_hash=ref_hash,
                canon_version_at_render=version,
                status=ShotStatus.PLANNED,
            )
            spec = _spec_from_shot(shot, shot_hash=new_hash, ref_hash=ref_hash)
            await container.enqueue_regen(spec)
            regenerated.append(shot.id)

    # 3. Fan out the regens; announce regen_done per shot as each completes (§5.6).
    if regenerated:
        container.spawn(_run_regens(container, book_id, regenerated))

    logger.info(
        "director.canon_edit",
        book_id=book_id,
        entity_key=body.entity_key,
        version=version,
        regenerated=len(regenerated),
        skipped=skipped,
    )
    return CanonEditResponse(
        entity_key=body.entity_key,
        version=version,
        affected_shot_ids=regenerated,
        skipped_shots=skipped,
    )


async def _run_regens(container: Container, book_id: str, shot_ids: list[str]) -> None:
    """Background: regen each dependent shot and publish ``regen_done`` (§5.6/§8.7)."""
    channel = book_channel(book_id)
    for shot_id in shot_ids:
        try:
            outcome = await container.run_regen(book_id, shot_id, None)
        except Exception as exc:  # noqa: BLE001 - one shot failing must not stall the rest
            logger.warning("director.regen_failed", shot_id=shot_id, error=str(exc))
            continue
        await container.redis.publish(
            channel,
            {
                "event": "regen_done",
                "shot_id": outcome.shot_id,
                "oss_url": outcome.oss_url,
                "qa": outcome.qa,
            },
        )


@router.post("/sessions/{session_id}/conflict_choice", response_model=ConflictChoiceResponse)
async def conflict_choice(
    session_id: str,
    body: ConflictChoiceRequest,
    container: ContainerDep,
    user: CurrentUser,
    _rl: Annotated[None, Depends(write_rate_limit)],
) -> ConflictChoiceResponse:
    """Record the Director's resolution of a surfaced conflict and announce it (§7.2)."""
    await _owned_session(container, user, session_id)
    await container.redis.set_json(
        f"kinora:conflict:{session_id}:{body.conflict_id}",
        {"option": body.option, "user_id": user.id},
        ttl_s=86_400,
    )
    await container.redis.publish(
        session_channel(session_id),
        {
            "event": "agent_activity",
            "agent": "showrunner",
            "message": f"conflict {body.conflict_id} resolved: {body.option}",
            "conflict": {"conflict_id": body.conflict_id, "option": body.option},
        },
    )
    logger.info(
        "director.conflict_choice",
        session_id=session_id,
        conflict_id=body.conflict_id,
        option=body.option,
    )
    return ConflictChoiceResponse(conflict_id=body.conflict_id, option=body.option)


__all__ = ["router"]
