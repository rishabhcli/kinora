"""Mutation resolvers — the gateway's write surface (§4/§5.4/§7.2/§8.7).

These reimplement the REST control logic against the public GraphQL inputs,
sharing the *same* container collaborators the REST routes use (the
:class:`~app.scheduler.intent.IntentController`, the comment classifier, the
canon service, the render enqueuer) so behaviour matches and ``KINORA_LIVE_VIDEO``
stays gated exactly as in REST. Every mutation enforces a scope and the
owner-boundary; failures surface as masked GraphQL errors.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, or_, select

from app.db.base import new_id
from app.db.hashing import compute_shot_hash, rotate_seed
from app.db.models.enums import EntityType, SessionMode, ShotStatus
from app.db.models.shot import Shot
from app.db.repositories.session import SessionRepo
from app.db.repositories.shot import ShotRepo
from app.graphql.context import GraphQLContext
from app.graphql.errors import bad_input, not_found
from app.graphql.execute import ResolveInfo
from app.graphql.resolvers.book import load_book
from app.graphql.resolvers.session import load_session_row
from app.memory.cache_service import CacheService
from app.memory.canon_service import CanonService
from app.memory.interfaces import ShotSpec
from app.scheduler.intent import SessionNotFoundError


def _mode(value: str | None) -> SessionMode | None:
    if value is None:
        return None
    try:
        return SessionMode(value)
    except ValueError as exc:
        raise bad_input(f"Unknown session mode {value!r}.") from exc


async def resolve_create_session(
    source: Any, args: dict[str, Any], ctx: GraphQLContext, info: ResolveInfo
) -> Any:
    """``createReadingSession`` — open a session against an owned book (§4.9)."""
    ctx.require("sessions:write")
    inp = args["input"]
    book = await load_book(ctx, str(inp["bookId"]))
    mode = _mode(inp.get("mode")) or SessionMode.VIEWER
    session_id = f"sess_{new_id()[:16]}"
    container = ctx.container
    async with container.session_factory() as session:
        await SessionRepo(session).upsert(
            session_id=session_id,
            book_id=book.id,
            user_id=ctx.user_id,
            focus_word=int(inp.get("focusWord") or 0),
            mode=mode,
        )
    async with container.session_factory() as session:
        controller = container.build_intent_controller(session)
        await controller.ensure_session(
            session_id, book.id, focus_word=int(inp.get("focusWord") or 0), mode=mode
        )
    return await load_session_row(ctx, session_id)


async def resolve_update_intent(
    source: Any, args: dict[str, Any], ctx: GraphQLContext, info: ResolveInfo
) -> Any:
    """``updateIntent`` — apply a debounced intent update + one control tick (§4.9)."""
    ctx.require("sessions:write")
    inp = args["input"]
    row = await load_session_row(ctx, str(inp["sessionId"]))
    velocity = float(inp.get("velocity") if inp.get("velocity") is not None else 4.0)
    if velocity < 0 or velocity > 1000.0:
        raise bad_input("`velocity` must be between 0 and 1000.")
    container = ctx.container
    async with container.session_factory() as session:
        controller = container.build_intent_controller(session)
        result = await controller.handle_intent_update(
            row.id,
            int(inp["focusWord"]),
            velocity,
            _mode(inp.get("mode")),
            book_id=row.book_id,
        )
    tick = result.tick
    return {
        "sessionId": row.id,
        "settled": result.settled,
        "allowPromotion": result.allow_promotion,
        "idle": tick.idle if tick else False,
        "bursting": tick.bursting if tick else False,
        "committedSecondsAhead": tick.committed_seconds_ahead if tick else 0.0,
        "promoted": list(tick.promoted) if tick else [],
        "keyframed": list(tick.keyframed) if tick else [],
        "cancelled": tick.cancelled if tick else 0,
    }


async def resolve_seek(
    source: Any, args: dict[str, Any], ctx: GraphQLContext, info: ResolveInfo
) -> Any:
    """``seek`` — jump to a word: cancel distant work, bridge, re-seed (§4.8)."""
    ctx.require("sessions:write")
    inp = args["input"]
    row = await load_session_row(ctx, str(inp["sessionId"]))
    word = int(inp["word"])
    if word < 0:
        raise bad_input("`word` must be non-negative.")
    container = ctx.container
    async with container.session_factory() as session:
        controller = container.build_intent_controller(session)
        try:
            result = await controller.handle_seek(row.id, word)
        except SessionNotFoundError as exc:
            raise not_found("Session has no live control state.") from exc
    return {
        "sessionId": row.id,
        "word": word,
        "cancelled": result.cancelled,
        "bridgeBeat": result.bridge_beat,
        "committedSecondsAhead": result.session.committed_seconds_ahead,
    }


async def resolve_director_comment(
    source: Any, args: dict[str, Any], ctx: GraphQLContext, info: ResolveInfo
) -> Any:
    """``directorComment`` — classify a note, enqueue a regen, learn a prior (§5.4)."""
    ctx.require("director:write")
    inp = args["input"]
    note = str(inp["note"]).strip()
    if not note or len(note) > 2000:
        raise bad_input("`note` must be 1–2000 characters.")
    row = await load_session_row(ctx, str(inp["sessionId"]))
    container = ctx.container
    async with container.session_factory() as session:
        shot = await ShotRepo(session).get(str(inp["shotId"]))
    if shot is None or shot.book_id != row.book_id:
        raise not_found("No such shot in this session's book.")

    shot_context = f"render_mode={shot.render_mode}; beat={shot.beat_id}; scene={shot.scene_id}"
    route = await container.classify_comment(note, shot_context=shot_context)

    new_seed = rotate_seed(shot.seed)
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
            shot.id,
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
    from app.queue.redis_queue import session_channel

    job_id = await container.enqueue_regen(spec)
    learned = await container.record_note_prefs(note, user_id=ctx.user_id, book_id=row.book_id)
    await container.redis.publish(
        session_channel(row.id),
        {
            "event": "agent_activity",
            "agent": route.agent,
            "aspect": route.aspect,
            "message": route.message,
            "shot_id": shot.id,
            "job_id": job_id or None,
        },
    )
    return {
        "shotId": shot.id,
        "agent": route.agent,
        "aspect": route.aspect,
        "message": route.message,
        "jobId": job_id or None,
        "learned": [_prior_json(p) for p in learned],
    }


async def resolve_edit_canon(
    source: Any, args: dict[str, Any], ctx: GraphQLContext, info: ResolveInfo
) -> Any:
    """``editCanon`` — edit an entity, surgically regen only the dependents (§8.7)."""
    ctx.require("canon:write")
    inp = args["input"]
    book = await load_book(ctx, str(inp["bookId"]))
    entity_key = str(inp["entityKey"])
    changes: dict[str, Any] = dict(inp.get("changes") or {})
    valid_from_beat = inp.get("validFromBeat")
    container = ctx.container

    async with container.session_factory() as session:
        canon = CanonService(
            session, embedder=container._embedder(), blob_store=container.object_store
        )
        current = await canon.get_entity(book.id, entity_key)
        if current is None:
            raise not_found("No such canon entity.")
        version = await canon.upsert_entity(
            book_id=book.id,
            entity_key=entity_key,
            entity_type=EntityType(current.type),
            name=str(changes.get("name", current.name)),
            valid_from_beat=(
                valid_from_beat if valid_from_beat is not None else current.valid_from_beat
            ),
            aliases=changes.get("aliases", current.aliases),
            description=changes.get("description", current.description),
            appearance=changes.get("appearance", current.appearance),
            voice=changes.get("voice", current.voice),
            style_tokens=changes.get("style_tokens", current.style_tokens),
        )

    regenerated: list[str] = []
    async with container.session_factory() as session:
        repo = ShotRepo(session)
        ref = func.jsonb_array_elements_text(Shot.reference_image_ids).table_valued(
            "value", name="ref"
        )
        references_entity = (
            select(1)
            .select_from(ref)
            .where(
                or_(
                    ref.c.value == entity_key,
                    func.split_part(ref.c.value, "@", 1) == entity_key,
                )
            )
            .exists()
        )
        total_shots = int(
            (
                await session.execute(
                    select(func.count()).select_from(Shot).where(Shot.book_id == book.id)
                )
            ).scalar_one()
        )
        rows = list(
            (await session.execute(select(Shot).where(Shot.book_id == book.id, references_entity)))
            .scalars()
            .all()
        )
        for shot in rows:
            if not _references_entity(shot.reference_image_ids, entity_key):
                continue
            ref_hash = CacheService.reference_set_hash(list(shot.reference_image_ids or []))
            new_hash = compute_shot_hash(
                book_id=book.id,
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
            spec = ShotSpec(
                book_id=shot.book_id,
                beat_id=shot.beat_id or "",
                scene_id=shot.scene_id,
                shot_id=shot.id,
                render_mode=shot.render_mode or "reference_to_video",
                seed=int(shot.seed or 0),
                reference_image_ids=list(shot.reference_image_ids or []),
                canon_version_at_render=version,
                target_duration_s=float(shot.duration_s or 5.0),
                reference_set_hash=ref_hash,
                shot_hash=new_hash,
            )
            await container.enqueue_regen(spec)
            regenerated.append(shot.id)

    from app.queue.redis_queue import book_channel

    skipped = total_shots - len(regenerated)
    await container.record_edit_prefs(changes, user_id=ctx.user_id, book_id=book.id)
    n = len(regenerated)
    blast = f"{n} shot{'' if n == 1 else 's'} re-rendering" if n else "no dependent shots"
    await container.redis.publish(
        book_channel(book.id),
        {
            "event": "agent_activity",
            "agent": "continuity_supervisor",
            "aspect": "canon",
            "message": f"{changes.get('name', current.name)} → v{version} · {blast}",
        },
    )
    return {
        "entityKey": entity_key,
        "version": version,
        "affectedShotIds": regenerated,
        "skippedShots": skipped,
    }


async def resolve_resolve_conflict(
    source: Any, args: dict[str, Any], ctx: GraphQLContext, info: ResolveInfo
) -> Any:
    """``resolveConflict`` — record + apply a §7.2 conflict resolution (idempotent)."""
    ctx.require("director:write")
    from app.memory.conflict_log import record_conflict_history
    from app.queue.redis_queue import (
        conflict_choice_key,
        conflict_object_key,
        session_channel,
    )

    inp = args["input"]
    row = await load_session_row(ctx, str(inp["sessionId"]))
    conflict_id = str(inp["conflictId"])
    option = str(inp["option"])
    container = ctx.container

    raw = await container.redis.get_json(conflict_object_key(row.id, conflict_id))
    conflict: dict[str, Any] | None = raw if isinstance(raw, dict) else None
    shot_id = _conflict_shot_id(conflict, conflict_id)
    reasoning = _choice_reasoning(option, shot_id=shot_id)

    prior = await container.redis.get_json(conflict_choice_key(row.id, conflict_id))
    if isinstance(prior, dict) and prior.get("applied"):
        return {
            "conflictId": conflict_id,
            "option": option,
            "status": "already_resolved",
            "shotId": shot_id,
            "reasoning": str(prior.get("reasoning") or reasoning),
        }

    will_apply = conflict is not None and shot_id is not None and option != "surface_to_user"
    if will_apply:
        status = "applied"
    elif option == "surface_to_user":
        status = "deferred"
    else:
        status = "recorded"
    await container.redis.set_json(
        conflict_choice_key(row.id, conflict_id),
        {"option": option, "user_id": ctx.user_id, "reasoning": reasoning, "applied": will_apply},
        ttl_s=86_400,
    )
    await record_conflict_history(
        container.redis,
        row.id,
        conflict=conflict,
        conflict_id=conflict_id,
        option=option,
        reasoning=reasoning,
    )
    await container.redis.publish(
        session_channel(row.id),
        {
            "event": "agent_activity",
            "agent": "showrunner",
            "message": reasoning,
            "shot_id": shot_id,
            "conflict": {"conflict_id": conflict_id, "option": option, "reasoning": reasoning},
        },
    )
    return {
        "conflictId": conflict_id,
        "option": option,
        "status": status,
        "shotId": shot_id,
        "reasoning": reasoning,
    }


def _references_entity(reference_image_ids: list[str] | None, entity_key: str) -> bool:
    for ref in reference_image_ids or []:
        if ref == entity_key or ref.split("@", 1)[0] == entity_key:
            return True
    return False


def _conflict_shot_id(conflict: dict[str, Any] | None, conflict_id: str) -> str | None:
    if conflict and conflict.get("shot_id"):
        return str(conflict["shot_id"])
    if conflict_id.startswith("cf_"):
        return conflict_id[3:] or None
    return None


def _choice_reasoning(option: str, *, shot_id: str | None) -> str:
    shot = f"shot {shot_id}" if shot_id else "the shot"
    if option == "honor_canon":
        return f"Director chose to honour canon — regenerating {shot} without the contradiction."
    if option == "evolve_canon":
        return f"Director chose to evolve canon — asserting the new state and regenerating {shot}."
    if option == "surface_to_user":
        return "Director deferred — leaving the conflict surfaced for now."
    return f"Director resolved the conflict: {option}."


def _prior_json(prior: Any) -> dict[str, Any]:
    value = prior.value if isinstance(prior.value, dict) else {}
    return {
        "kind": prior.kind,
        "weight": prior.weight,
        "note": value.get("note") if isinstance(value, dict) else None,
    }


__all__ = [
    "resolve_create_session",
    "resolve_director_comment",
    "resolve_edit_canon",
    "resolve_resolve_conflict",
    "resolve_seek",
    "resolve_update_intent",
]
