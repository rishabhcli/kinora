#!/usr/bin/env python3
"""Simulate the §7.2 "lost-sword" continuity conflict in a live reading session.

For the §16 demo (step 4): inject the canonical Continuity-Supervisor conflict
onto a session's event channel **exactly as the render worker does when it
surfaces one** (kinora.md §7.2) — persisting the structured conflict object so
the Director's pick is actually applied — so the Crew-dispute modal pops in the
running app and the resolve → regenerate (or evolve-canon) loop runs end-to-end,
without needing the Critic to flag a real timeline violation first.

    # newest reading session for a book (open the book in the app first)
    backend/.venv/bin/python backend/scripts/simulate_conflict.py <book_id>

    # or target a specific session / shot
    backend/.venv/bin/python backend/scripts/simulate_conflict.py --session <session_id>
    backend/.venv/bin/python backend/scripts/simulate_conflict.py <book_id> --shot <shot_id>

Talks to the configured Redis + Postgres (``app.core.config.Settings``). Safe with
``KINORA_LIVE_VIDEO`` off — the resolution regenerates a zero-spend Ken-Burns clip.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

# The canonical demo conflict (kinora.md §7.2 — the Track-3 money shot).
CLAIM = "the heroine draws a sword she lost"
CANON_FACT = "state_hero_sword_001 retired at beat_0034 (sword lost in the river)"
OPTIONS = [
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
]


async def _run(book_id: str | None, session_id: str | None, shot_id: str | None) -> int:
    from sqlalchemy import select

    from app.composition import build_container
    from app.core.config import get_settings
    from app.db.models.session import Session
    from app.db.models.shot import Shot
    from app.queue.redis_queue import conflict_object_key, session_channel

    container = build_container(get_settings())
    try:
        async with container.session_factory() as db:
            # Resolve the live session: explicit, else the book's newest.
            if session_id is None:
                if book_id is None:
                    print("error: pass a book_id or --session SESSION_ID", file=sys.stderr)
                    return 2
                session = (
                    await db.execute(
                        select(Session)
                        .where(Session.book_id == book_id)
                        .order_by(Session.created_at.desc())
                        .limit(1)
                    )
                ).scalar_one_or_none()
                if session is None:
                    print(
                        f"error: no reading session for book {book_id} — open it in the app first",
                        file=sys.stderr,
                    )
                    return 1
            else:
                session = await db.get(Session, session_id)
                if session is None:
                    print(f"error: no such session {session_id}", file=sys.stderr)
                    return 1
            session_id, book_id = session.id, session.book_id

            # Resolve a real shot to dispute: explicit, else the book's newest.
            if shot_id is not None:
                shot = await db.get(Shot, shot_id)
            else:
                shot = (
                    await db.execute(
                        select(Shot)
                        .where(Shot.book_id == book_id)
                        .order_by(Shot.created_at.desc())
                        .limit(1)
                    )
                ).scalar_one_or_none()
            if shot is None:
                print(
                    f"error: book {book_id} has no shots — seed/ingest it first (make seed-demo)",
                    file=sys.stderr,
                )
                return 1
            shot_id, beat_id = shot.id, shot.beat_id

        conflict_id = f"cf_{shot_id}"
        conflict = {
            "conflict_id": conflict_id,
            "raised_by": "continuity_supervisor",
            "type": "canon_violation",
            "shot_id": shot_id,
            "claim": CLAIM,
            "canon_fact": CANON_FACT,
            "current_beat": beat_id,
            "contradicting_state_id": None,
            "user_facing": True,
            "options": OPTIONS,
        }

        # Persist it so POST /sessions/{id}/conflict_choice applies the pick (§7.2).
        await container.redis.set_json(
            conflict_object_key(session_id, conflict_id), conflict, ttl_s=86_400
        )

        channel = session_channel(session_id)
        # Surface it exactly as worker._publish_render_events does (§5.6).
        await container.redis.publish(
            channel,
            {
                "event": "conflict_choice",
                "conflict_id": conflict_id,
                "options": OPTIONS,
                "claim": CLAIM,
                "canon_fact": CANON_FACT,
                "current_beat": beat_id,
                "raised_by": "continuity_supervisor",
                "shot_id": shot_id,
            },
        )
        await container.redis.publish(
            channel,
            {
                "event": "agent_activity",
                "agent": "continuity_supervisor",
                "message": f"Continuity conflict: {CLAIM}",
                "conflict": conflict,
                "shot_id": shot_id,
            },
        )
        print(
            f"✓ surfaced {conflict_id} on session {session_id} (shot {shot_id}).\n"
            "  The Crew-dispute modal should appear in the app — pick an option to "
            "resolve it live."
        )
        return 0
    finally:
        await container.redis.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Simulate the §7.2 lost-sword continuity conflict in a live reading session."
    )
    parser.add_argument("book_id", nargs="?", help="Book whose newest reading session to target.")
    parser.add_argument("--session", dest="session_id", help="Target a specific session id.")
    parser.add_argument("--shot", dest="shot_id", help="Dispute a specific shot (else newest).")
    args = parser.parse_args()
    return asyncio.run(_run(args.book_id, args.session_id, args.shot_id))


if __name__ == "__main__":
    raise SystemExit(main())
