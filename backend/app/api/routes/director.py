"""Director routes — region comments, surgical canon edits, conflict choices (§5.4).

* ``POST /sessions/{id}/comment`` classifies a region note with a cheap chat()
  call (Cinematographer pacing/look vs Continuity room/canon, §5.4), enqueues a
  targeted regen of the commented shot, and emits an ``agent_activity`` event.
* ``POST /books/{id}/canon_edit`` writes the entity edit via
  ``canon_service.upsert_entity``, then finds the shots whose reference set
  includes the changed entity, recomputes their ``shot_hash``, and enqueues a
  regen for **only those** (surgical re-render, §8.7), emitting ``regen_done`` as
  each completes — everything else still hits the cache.
* ``POST /sessions/{id}/conflict_choice`` applies the Director's resolution of a
  surfaced conflict (§7.2): it streams the Showrunner's arbitration into the feed,
  then regenerates the disputed shot (``honor_canon``) or writes the new state and
  regenerates (``evolve_canon``), closing the loop with a ``regen_done`` event.
"""

from __future__ import annotations

import asyncio
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from sqlalchemy import select

from app.api.deps import ContainerDep, CurrentUser, write_rate_limit
from app.api.errors import APIError
from app.api.routes.prefs import prior_view
from app.api.routes.sessions import _owned_session
from app.api.schemas import (
    CanonEditRequest,
    CanonEditResponse,
    CommentRequest,
    CommentResponse,
    ConflictChoiceRequest,
    ConflictChoiceResponse,
    ConflictRecordResponse,
)
from app.composition import Container
from app.core.logging import get_logger
from app.db.hashing import compute_shot_hash
from app.db.models.enums import EntityType, ShotStatus
from app.db.models.shot import Shot
from app.db.repositories.beat import BeatRepo
from app.db.repositories.book import BookRepo
from app.db.repositories.shot import ShotRepo
from app.memory.cache_service import CacheService
from app.memory.canon_service import CanonService
from app.memory.conflict_log import load_conflict_history, project_record, record_conflict_history
from app.memory.interfaces import ShotSpec
from app.observability import metrics
from app.queue.redis_queue import (
    book_channel,
    conflict_choice_key,
    conflict_object_key,
    session_channel,
)

logger = get_logger("app.api.director")

router = APIRouter(tags=["director"])

# Golden-ratio seed step: a Director regen re-rolls to a genuinely new variation.
_SEED_STEP = 0x9E3779B1
_SEED_MASK = 0x7FFFFFFF

# Beat between staged crew lines while a conflict resolves, so the §5.4 feed
# streams the §7.2 resolution as the crew works rather than in one jump (§16 demo).
_STAGE_DELAY_S = 0.3


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
            body.shot_id,
            seed=new_seed,
            shot_hash=new_hash,
            reference_set_hash=ref_hash,
            status=ShotStatus.PROMOTED,
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

    # Learn the reader's directing taste from the note (§8.6): a "slower" comment
    # nudges the book's pacing prior so the next session defaults that way.
    learned = await container.record_note_prefs(body.note, user_id=user.id, book_id=row.book_id)

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
        "director.comment",
        session_id=session_id,
        shot_id=body.shot_id,
        agent=route.agent,
        learned=[p.kind for p in learned],
    )
    return CommentResponse(
        shot_id=body.shot_id,
        agent=route.agent,
        aspect=route.aspect,
        message=route.message,
        job_id=job_id or None,
        learned=[prior_view(p) for p in learned],
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
            (await session.execute(select(Shot).where(Shot.book_id == book_id))).scalars().all()
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

    # Announce the edit on the live crew feed (§5.4) — the Continuity Supervisor's
    # surgical write + its blast radius, on the book channel both shells subscribe
    # to. Everything not in this count stayed a cache hit (§8.7).
    entity_name = str(changes.get("name", current.name))
    n = len(regenerated)
    blast = f"{n} shot{'' if n == 1 else 's'} re-rendering" if n else "no dependent shots"
    await container.redis.publish(
        book_channel(book_id),
        {
            "event": "agent_activity",
            "agent": "continuity_supervisor",
            "aspect": "canon",
            "message": f"{entity_name} → v{version} · {blast}",
        },
    )

    # Learn from the edit's content (§8.6): a re-coloured/re-framed entity shifts
    # the palette/composition prior for this book's future shots.
    await container.record_edit_prefs(changes, user_id=user.id, book_id=book_id)

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
    """Apply the Director's resolution of a surfaced conflict (§7.2).

    Records the pick, streams the Showrunner's arbitration reasoning into the feed,
    and **acts on it**: ``honor_canon`` regenerates the shot honouring the canon,
    ``evolve_canon`` writes the new state then regenerates, ``surface_to_user``
    leaves it surfaced. The affected shot's fresh clip closes the loop (a
    ``regen_done`` event), so the §16 demo resolves a conflict live.
    """
    row = await _owned_session(container, user, session_id)
    option = str(body.option)  # ConflictOption is a StrEnum — take the wire value.

    raw = await container.redis.get_json(conflict_object_key(session_id, body.conflict_id))
    conflict: dict[str, Any] | None = raw if isinstance(raw, dict) else None
    shot_id = _conflict_shot_id(conflict, body.conflict_id)
    claim = (conflict or {}).get("claim")
    canon_fact = (conflict or {}).get("canon_fact")
    reasoning = _choice_reasoning(option, shot_id=shot_id, claim=claim, canon_fact=canon_fact)

    # Idempotency: a repeat pick for an already-applied conflict is a no-op — a
    # double-tap can't trigger a second regen or re-evolve canon (§7.2).
    prior = await container.redis.get_json(conflict_choice_key(session_id, body.conflict_id))
    if isinstance(prior, dict) and prior.get("applied"):
        return ConflictChoiceResponse(
            conflict_id=body.conflict_id,
            option=body.option,
            status="already_resolved",
            shot_id=shot_id,
            reasoning=str(prior.get("reasoning") or reasoning),
        )

    will_apply = conflict is not None and shot_id is not None and option != "surface_to_user"
    if will_apply:
        status = "applied"
    elif option == "surface_to_user":
        status = "deferred"
    else:
        status = "recorded"

    # Record the pick (idempotency marker) + the durable history record (§7.2).
    await container.redis.set_json(
        conflict_choice_key(session_id, body.conflict_id),
        {"option": option, "user_id": user.id, "reasoning": reasoning, "applied": will_apply},
        ttl_s=86_400,
    )
    await record_conflict_history(
        container.redis,
        session_id,
        conflict=conflict,
        conflict_id=body.conflict_id,
        option=option,
        reasoning=reasoning,
    )
    metrics.inc_conflict_resolved(option)

    # The Showrunner's arbitration of the pick — the decision record kept in the
    # feed (and streamed into the open dispute). Published first so a subscriber
    # always sees the decision before the staged execution + regen (§7.2).
    await container.redis.publish(
        session_channel(session_id),
        {
            "event": "agent_activity",
            "agent": "showrunner",
            "message": reasoning,
            "shot_id": shot_id,
            "conflict": {
                "conflict_id": body.conflict_id,
                "option": option,
                "reasoning": reasoning,
                "claim": claim,
                "canon_fact": canon_fact,
            },
        },
    )

    # Apply the decision against the real shot (staged crew execution → fresh clip).
    if will_apply:
        assert shot_id is not None and conflict is not None  # narrowed by will_apply
        container.spawn(
            _apply_conflict_choice(
                container,
                session_id=session_id,
                book_id=row.book_id,
                shot_id=shot_id,
                option=option,
                conflict=conflict,
                conflict_id=body.conflict_id,
            )
        )

    logger.info(
        "director.conflict_choice",
        session_id=session_id,
        conflict_id=body.conflict_id,
        option=option,
        status=status,
        shot_id=shot_id,
    )
    return ConflictChoiceResponse(
        conflict_id=body.conflict_id,
        option=body.option,
        status=status,
        shot_id=shot_id,
        reasoning=reasoning,
    )


def _conflict_shot_id(conflict: dict[str, Any] | None, conflict_id: str) -> str | None:
    """The disputed shot id — from the persisted conflict, else the ``cf_<shot>`` id."""
    if conflict and conflict.get("shot_id"):
        return str(conflict["shot_id"])
    if conflict_id.startswith("cf_"):
        return conflict_id[3:] or None
    return None


def _choice_reasoning(
    option: str, *, shot_id: str | None, claim: str | None, canon_fact: str | None
) -> str:
    """The Showrunner's one-line arbitration for the Director's pick (§7.2)."""
    shot = f"shot {shot_id}" if shot_id else "the shot"
    if option == "honor_canon":
        text = f"Director chose to honour canon — regenerating {shot} without the contradiction"
        if claim:
            text += f" ({claim})"
        if canon_fact:
            text += f"; respecting {canon_fact}"
        return text + "."
    if option == "evolve_canon":
        text = f"Director chose to evolve canon — asserting the new state and regenerating {shot}"
        if claim:
            text += f" ({claim})"
        return text + "."
    if option == "surface_to_user":
        return "Director deferred — leaving the conflict surfaced for now."
    return f"Director resolved the conflict: {option}."


async def _stage(
    container: Container,
    channel: str,
    agent: str,
    message: str,
    decision: dict[str, Any],
    shot_id: str,
) -> None:
    """Publish one staged crew line + a beat of delay, so the feed streams the
    §7.2 resolution as the crew works rather than in a single jump (§16 demo)."""
    await container.redis.publish(
        channel,
        {
            "event": "agent_activity",
            "agent": agent,
            "message": message,
            "shot_id": shot_id,
            "conflict": decision,
        },
    )
    await asyncio.sleep(_STAGE_DELAY_S)


async def _apply_conflict_choice(
    container: Container,
    *,
    session_id: str,
    book_id: str,
    shot_id: str,
    option: str,
    conflict: dict[str, Any],
    conflict_id: str,
) -> None:
    """Background: apply the §7.2 decision — stage the crew's execution (so the feed
    shows them working), evolve canon when chosen, then regenerate the disputed
    shot, announcing ``regen_done`` once the loop closes."""
    channel = session_channel(session_id)
    decision = {"conflict_id": conflict_id, "option": option}
    try:
        await _stage(
            container,
            channel,
            "continuity_supervisor",
            "Re-checking the canon graph for the contradiction…",
            decision,
            shot_id,
        )
        if option == "evolve_canon":
            state_id = await _evolve_canon_for_choice(container, book_id=book_id, conflict=conflict)
            await _stage(
                container,
                channel,
                "continuity_supervisor",
                f"Canon evolved — wrote the new state ({state_id}) from this beat.",
                decision,
                shot_id,
            )
        else:
            await _stage(
                container,
                channel,
                "cinematographer",
                "Recomposing the shot — honouring the established canon.",
                decision,
                shot_id,
            )
        outcome = await container.run_regen(book_id, shot_id, session_id)
        await container.redis.publish(
            channel,
            {
                "event": "regen_done",
                "shot_id": outcome.shot_id,
                "oss_url": outcome.oss_url,
                "qa": outcome.qa,
            },
        )
    except Exception as exc:  # noqa: BLE001 - a failed apply must not crash the worker
        logger.warning(
            "director.conflict_apply_failed",
            session_id=session_id,
            shot_id=shot_id,
            option=option,
            error=str(exc),
        )


async def _evolve_canon_for_choice(
    container: Container, *, book_id: str, conflict: dict[str, Any]
) -> str:
    """Write the §8.5 canon evolution for an ``evolve_canon`` pick: re-assert the
    contradicting fact from the current beat when identifiable, else record a typed
    ``canon_evolved`` fact carrying the claim — either way a real versioned write."""
    claim = str(conflict.get("claim") or "the story changed")
    current_beat = conflict.get("current_beat")
    contradicting = conflict.get("contradicting_state_id")
    async with container.session_factory() as session:
        canon = CanonService(
            session, embedder=container._embedder(), blob_store=container.object_store
        )
        beat = await BeatRepo(session).get(str(current_beat)) if current_beat else None
        at = beat.beat_index if beat is not None else 0
        cited = None
        if contradicting:
            states = await canon.active_states_at_beat(book_id, at)
            cited = next((s for s in states if s.state_id == contradicting), None)
        source_span = {"note": claim[:200]}
        if cited is not None:
            return await canon.assert_state(
                book_id=book_id,
                subject_entity_key=cited.subject_entity_key,
                predicate=cited.predicate,
                object_value=cited.object_value,
                valid_from_beat=at,
                source_span=source_span,
            )
        return await canon.assert_state(
            book_id=book_id,
            subject_entity_key="story",
            predicate="canon_evolved",
            object_value=claim,
            valid_from_beat=at,
            source_span=source_span,
        )


@router.get("/sessions/{session_id}/conflicts", response_model=list[ConflictRecordResponse])
async def list_conflicts(
    session_id: str,
    container: ContainerDep,
    user: CurrentUser,
) -> list[ConflictRecordResponse]:
    """The session's §7.2 conflict log — surfaced disputes + their resolutions — so
    a refreshed client reloads the Crew-dispute state instead of losing it."""
    await _owned_session(container, user, session_id)
    records = await load_conflict_history(container.redis, session_id)
    return [ConflictRecordResponse(**r) for r in records]


def _demo_conflict_object(*, shot_id: str, beat_id: str | None) -> dict[str, Any]:
    """The canonical §7.2 lost-sword conflict object used by the demo trigger."""
    return {
        "conflict_id": f"cf_{shot_id}",
        "raised_by": "continuity_supervisor",
        "type": "canon_violation",
        "shot_id": shot_id,
        "claim": "the heroine draws a sword she lost",
        "canon_fact": "state_hero_sword_001 retired at beat_0034 (sword lost in the river)",
        "current_beat": beat_id,
        "contradicting_state_id": None,
        "user_facing": True,
        "options": [
            {
                "id": "honor_canon",
                "action": "regenerate the shot honouring the established canon",
                "cost_video_s": 5.0,
            },
            {"id": "surface_to_user", "action": "ask the director to choose", "cost_video_s": 0.0},
            {
                "id": "evolve_canon",
                "action": "assert the new state and regenerate",
                "requires": "textual support",
            },
        ],
    }


@router.post("/sessions/{session_id}/demo/conflict", response_model=ConflictRecordResponse)
async def demo_conflict(
    session_id: str,
    container: ContainerDep,
    user: CurrentUser,
    _rl: Annotated[None, Depends(write_rate_limit)],
) -> ConflictRecordResponse:
    """DEV-ONLY (§16 demo): surface the canonical lost-sword §7.2 conflict on this
    session so the Crew-dispute modal appears live — without waiting for the Critic
    to flag a real timeline violation. Gated to the local environment."""
    if not container.settings.is_local:
        raise APIError("forbidden", "the demo conflict trigger is local-only", status=403)
    row = await _owned_session(container, user, session_id)

    async with container.session_factory() as session:
        shot = (
            await session.execute(
                select(Shot)
                .where(Shot.book_id == row.book_id)
                .order_by(Shot.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
    if shot is None:
        raise APIError("no_shots", "this book has no shots to dispute yet", status=409)

    conflict = _demo_conflict_object(shot_id=shot.id, beat_id=shot.beat_id)
    await container.redis.set_json(
        conflict_object_key(session_id, conflict["conflict_id"]), conflict, ttl_s=86_400
    )
    await record_conflict_history(
        container.redis, session_id, conflict=conflict, conflict_id=conflict["conflict_id"]
    )
    channel = session_channel(session_id)
    await container.redis.publish(
        channel,
        {
            "event": "conflict_choice",
            "conflict_id": conflict["conflict_id"],
            "options": conflict["options"],
            "claim": conflict["claim"],
            "canon_fact": conflict["canon_fact"],
            "current_beat": conflict["current_beat"],
            "raised_by": conflict["raised_by"],
            "shot_id": shot.id,
        },
    )
    await container.redis.publish(
        channel,
        {
            "event": "agent_activity",
            "agent": "continuity_supervisor",
            "message": f"Continuity conflict: {conflict['claim']}",
            "conflict": conflict,
            "shot_id": shot.id,
        },
    )
    metrics.inc_conflict()
    logger.info("director.demo_conflict", session_id=session_id, shot_id=shot.id)
    return ConflictRecordResponse(**project_record(conflict))


__all__ = ["router"]
