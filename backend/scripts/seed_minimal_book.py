#!/usr/bin/env python3
"""Seed a minimal *ready* book (pages + one Ken-Burns clip) for shelf UX testing."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]

BOOKS = {
    "lrrh": {
        "book_id": "e2e0red0riding000000000000seed2",
        "title": "Little Red Riding Hood",
        "author": "Public domain",
        "art_direction": "woodland storybook",
        "pdf": _REPO_ROOT / "assets/books/little_red_riding_hood.pdf",
    },
}


async def seed_minimal(
    *,
    book_id: str,
    title: str,
    author: str,
    art_direction: str,
    pdf_path: Path,
    email: str,
    password: str,
) -> None:
    from seed_e2e import render_pages

    from app.api.security import hash_password
    from app.core.config import get_settings
    from app.db.hashing import compute_shot_hash
    from app.db.models.enums import BookStatus, ShotStatus
    from app.db.repositories.beat import BeatRepo
    from app.db.repositories.book import BookRepo, PageRepo
    from app.db.repositories.scene import SceneRepo
    from app.db.repositories.shot import ShotCacheRepo, ShotRepo, SourceSpanRepo
    from app.db.repositories.user import UserRepo
    from app.db.session import get_session
    from app.memory.cache_service import CacheService
    from app.redis.client import RedisClient
    from app.render.degrade import ken_burns_over_image
    from app.storage.object_store import ObjectStore, keys

    settings = get_settings()
    store = ObjectStore.from_settings(settings)
    redis = RedisClient.from_url(settings.redis_url)
    store.ensure_bucket()

    rendered = render_pages(pdf_path, num_pages=3)
    total_words = sum(len(p.word_boxes) for p in rendered)

    async with get_session() as session:
        users = UserRepo(session)
        user = await users.get_by_email(email)
        if user is None:
            user = await users.create(email=email, hashed_password=hash_password(password))
        user_id = user.id

    async with get_session() as session:
        existing = await BookRepo(session).get(book_id)
        if existing is not None:
            await session.delete(existing)

    page_image_keys: dict[int, str] = {}
    for page in rendered:
        key = f"pages/{book_id}/{page.page_number}.png"
        store.put_bytes(key, page.png, "image/png")
        page_image_keys[page.page_number] = key

    async with get_session() as session:
        await BookRepo(session).create(
            title=title,
            author=author,
            user_id=user_id,
            source_pdf_key=keys.pdf(book_id),
            status=BookStatus.READY,
            num_pages=len(rendered),
            art_direction=art_direction,
            book_id=book_id,
        )
        page_repo = PageRepo(session)
        for page in rendered:
            await page_repo.create(
                book_id=book_id,
                page_number=page.page_number,
                image_key=page_image_keys[page.page_number],
                text=page.text,
                word_boxes=page.word_boxes,
            )

    store.put_bytes(keys.pdf(book_id), pdf_path.read_bytes(), "application/pdf")

    scene_id = f"scene_{book_id[:16]}_00"
    beat_id = f"beat_{book_id[:16]}_00"
    async with get_session() as session:
        await SceneRepo(session).create(
            book_id=book_id,
            scene_index=0,
            page_start=1,
            page_end=1,
            title="Through the woods",
            style_entity_key=None,
            scene_id=scene_id,
        )
        await BeatRepo(session).create(
            book_id=book_id,
            scene_id=scene_id,
            beat_index=0,
            summary="Little Red Riding Hood sets out through the wood.",
            entities=[],
            described_visuals="woodland storybook scene",
            mood="gentle",
            source_span={"page": 1, "word_range": [0, 9]},
            beat_id=beat_id,
        )

    shot_id = f"shot_{book_id[:16]}_00"
    clip_key = keys.clip(book_id, shot_id)
    clip_bytes = ken_burns_over_image(rendered[0].png, duration_s=2.0, size=(320, 180), fps=24)
    store.put_bytes(clip_key, clip_bytes, "video/mp4")
    ref_hash = CacheService.reference_set_hash([])
    content_hash = compute_shot_hash(
        book_id=book_id,
        beat_id=beat_id,
        canon_version_at_render=1,
        render_mode="ken_burns_keyframe",
        seed=42,
        reference_set_hash=ref_hash,
    )

    last_frame_key = keys.lastframe(book_id, shot_id)
    store.put_bytes(last_frame_key, rendered[0].png, "image/png")

    async with get_session() as session:
        await ShotRepo(session).create(
            id=shot_id,
            book_id=book_id,
            scene_id=scene_id,
            beat_id=beat_id,
            source_span={"word_start": 0, "word_end": min(9, max(0, total_words - 1))},
            status=ShotStatus.ACCEPTED,
            render_mode="ken_burns_keyframe",
            prompt=f"{title}: opening beat.",
            negative_prompt="extra fingers, warped face",
            seed=42,
            reference_set_hash=ref_hash,
            reference_image_ids=[],
            duration_s=5.0,
            output={"clip_key": clip_key, "last_frame_key": last_frame_key},
            qa={"ccs": 0.9, "verdict": "pass"},
            shot_hash=content_hash,
            canon_version_at_render=1,
        )
        await ShotCacheRepo(session).put(
            shot_hash=content_hash,
            book_id=book_id,
            clip_key=clip_key,
            qa={"ccs": 0.9, "verdict": "pass"},
        )
        await SourceSpanRepo(session).bulk_insert(
            [
                {
                    "book_id": book_id,
                    "word_index_start": 0,
                    "word_index_end": min(9, max(0, total_words - 1)),
                    "shot_id": shot_id,
                    "scene_id": scene_id,
                    "beat_id": beat_id,
                }
            ]
        )

    owner_key = f"kinora:user:{user_id}:books"
    await redis.raw.sadd(owner_key, book_id)
    await redis.set_json(f"kinora:book:progress:{book_id}", {"stage": "ready", "pct": 1.0})
    await redis.close()
    print(f"seeded {title!r} ({book_id}) for {email}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Seed a minimal ready book for shelf UX.")
    parser.add_argument("--preset", choices=tuple(BOOKS.keys()), default="lrrh")
    parser.add_argument("--email", default="demo@kinora.local")
    parser.add_argument("--password", default="demo-password-123")
    args = parser.parse_args(argv)
    cfg = BOOKS[args.preset]
    pdf = cfg["pdf"]
    if not pdf.exists():
        print(f"PDF not found: {pdf}", file=sys.stderr)
        return 1
    from app.core.config import get_settings
    from app.core.logging import configure_logging

    configure_logging(get_settings().log_level)
    asyncio.run(
        seed_minimal(
            book_id=cfg["book_id"],
            title=cfg["title"],
            author=cfg["author"],
            art_direction=cfg["art_direction"],
            pdf_path=pdf,
            email=args.email,
            password=args.password,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
