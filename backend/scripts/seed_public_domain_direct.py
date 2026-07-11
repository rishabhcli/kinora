#!/usr/bin/env python3
"""Build public-domain books **directly** through the real repositories — each
``status=ready`` with real pages + per-word boxes, a populated source-span index,
a (bounded) shot grid, and one playable Ken-Burns clip — in a couple of seconds
each and with **zero model spend** (Agent 05).

This sidesteps the slow, 429-prone *real* ingest (which stalls full novels at the
canon stage), so the desktop library shelf is reliably populated and every book is
openable the moment it appears. It is the shared build engine
``scripts/seed_library_100.py`` drives over the 100+ book catalogue; run directly
it (re)seeds the original five demo public-domain titles.

    backend/.venv/bin/python backend/scripts/seed_public_domain_direct.py

Re-running is idempotent: each book has a fixed id (``pubdom{gid}``) that is wiped
and rebuilt. Agent 05 additions over the original: the cover is the per-title
designed cover from :mod:`app.library.covers`, stored at ``covers/{book_id}`` and
recorded on the new ``Book.cover_key`` (a real shelf cover, not just "page 1"); the
shot grid is bounded so a 130-book seed stays fast.
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
import zipfile
from pathlib import Path
from typing import Any

import httpx

# Reuse the proven PyMuPDF RenderedPage model. fitz opens EPUB directly (reflows
# into pages), so per-word extraction works the same on Gutenberg EPUBs.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from seed_e2e import RenderedPage  # noqa: E402

from app.library.catalog import book_id_for  # noqa: E402
from app.library.covers import make_typographic_cover  # noqa: E402

_REPO = Path(__file__).resolve().parents[2]
DEST = _REPO / "assets" / "books" / "public-domain"
EMAIL = "demo@kinora.local"
PASSWORD = "demo-password-123"

WORD_STEP = 12          # words per shot (the §4.2 source-span grid step)
SHOT_DURATION_S = 5.0
MAX_SHOTS = 48          # bound per-book shot rows so a 130-book seed stays fast

# (gutenberg_id, title, author, art_direction, pages_to_rasterise) — the original
# five demo titles, kept so this script is useful standalone.
BOOKS: list[tuple[int, str, str, str, int]] = [
    (1952, "The Yellow Wallpaper", "Charlotte Perkins Gilman",
     "painterly gothic, muted candlelight, unsettling stillness", 8),
    (5200, "The Metamorphosis", "Franz Kafka",
     "surreal muted realism, uneasy shadows, cold dawn light", 8),
    (43, "Dr Jekyll and Mr Hyde", "Robert Louis Stevenson",
     "victorian noir, gaslit fog, deep chiaroscuro", 8),
    (46, "A Christmas Carol", "Charles Dickens",
     "warm victorian, snow and hearthlight, candleglow", 8),
    (11, "Alice in Wonderland", "Lewis Carroll",
     "whimsical storybook, dreamlike, richly saturated", 8),
]

_FRONT_MATTER = (
    "project gutenberg", "gutenberg-tm", "ebook is for the use", "this ebook",
    "start of the project", "produced by", "character set encoding", "*** start",
)


_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15"
# Gutenberg bandwidth-throttles a busy IP and drops large transfers mid-body, so
# we try faster mirrors first and resume via HTTP Range across drops.
_MIRRORS = (
    "https://gutenberg.pglaf.org",
    "http://aleph.gutenberg.org",
    "https://www.gutenberg.org",
)


def _stream_resume(url: str, tmp: Path, timeout: float, max_attempts: int = 6) -> bool:
    """Download ``url`` into ``tmp``, resuming via Range on drops; True if complete."""
    tmp.unlink(missing_ok=True)
    got = 0
    total: int | None = None
    for _ in range(max_attempts):
        headers = {"User-Agent": _UA}
        if got:
            headers["Range"] = f"bytes={got}-"
        try:
            with httpx.stream(
                "GET", url, headers=headers, follow_redirects=True, timeout=timeout
            ) as r:
                if r.status_code not in (200, 206):
                    return False  # 404/410 etc. — caller tries the next mirror
                if r.status_code == 200 and got:
                    got = 0  # server ignored Range — restart this file
                    tmp.unlink(missing_ok=True)
                cl = r.headers.get("Content-Length")
                if cl:
                    total = int(cl) + (got if r.status_code == 206 else 0)
                with tmp.open("ab" if got else "wb") as fh:
                    for chunk in r.iter_bytes(65536):
                        fh.write(chunk)
                        got += len(chunk)
            if total and got >= total:
                break
        except Exception:  # noqa: BLE001 - drop/timeout: resume from what landed
            got = tmp.stat().st_size if tmp.exists() else 0
    return tmp.exists() and tmp.stat().st_size > 20000 and zipfile.is_zipfile(tmp)


def download(
    gid: int, url: str | None = None, *, timeout: float = 45.0, mirror_start: int = 0
) -> Path | None:
    """Download (and cache) a Gutenberg EPUB; idempotent (skips a good cached file).

    Tries faster mirrors then the main site, the ``-images`` variant as a fallback,
    each Range-resumed across Gutenberg's mid-transfer drops and validated as a real
    EPUB (zip) before it is accepted — so a throttled/flaky connection still
    eventually lands a complete file. ``mirror_start`` rotates which mirror is tried
    first, so concurrent prefetch spreads load across hosts (no per-host cutoff).
    """
    DEST.mkdir(parents=True, exist_ok=True)
    out = DEST / f"pg{gid}.epub"
    if out.exists() and out.stat().st_size > 20000:
        return out
    tmp = out.with_suffix(".part")
    n = len(_MIRRORS)
    mirrors = [_MIRRORS[(mirror_start + i) % n] for i in range(n)]
    candidates = [
        f"{base}/cache/epub/{gid}/pg{gid}{suffix}.epub"
        for base in mirrors
        for suffix in ("", "-images")
    ]
    if url:
        candidates.append(url)
    for candidate in candidates:
        if _stream_resume(candidate, tmp, timeout):
            tmp.replace(out)
            return out
    tmp.unlink(missing_ok=True)
    return None


def extract_story_pages(epub: Path, num_pages: int) -> list[RenderedPage]:
    """Per-word boxes + text for ``num_pages`` *story* pages, skipping the
    Gutenberg license/front-matter so the reader opens on actual prose."""
    import fitz  # PyMuPDF

    doc = fitz.open(str(epub))
    try:
        start = 0
        for i in range(doc.page_count):
            t = doc[i].get_text("text")
            low = t.lower()
            if len(t.strip()) >= 1200 and not any(k in low for k in _FRONT_MATTER):
                start = i
                break
        pages: list[RenderedPage] = []
        global_word = 0
        for offset in range(num_pages):
            page = doc[min(start + offset, doc.page_count - 1)]
            rect = page.rect
            width = float(rect.width) or 1.0
            height = float(rect.height) or 1.0
            boxes: list[dict[str, Any]] = []
            parts: list[str] = []
            for w in page.get_text("words"):
                x0, y0, x1, y1, txt = w[0], w[1], w[2], w[3], w[4]
                if not txt.strip():
                    continue
                boxes.append({
                    "word_index": global_word, "text": txt,
                    "bbox": [round(x0 / width, 6), round(y0 / height, 6),
                             round(max(0.0, x1 - x0) / width, 6),
                             round(max(0.0, y1 - y0) / height, 6)],
                })
                parts.append(txt)
                global_word += 1
            pages.append(RenderedPage(page_number=offset + 1, png=b"",
                                      text=" ".join(parts), word_boxes=boxes))
        return pages
    finally:
        doc.close()


async def build_book(
    *, book_id: str, tag: str, title: str, author: str, art: str, epub: Path,
    num_pages: int, cover_png: bytes | None = None, cover_source: str = "generated",
    max_shots: int = MAX_SHOTS,
) -> dict[str, Any] | None:
    """Write one ready, span-indexed book directly without pre-rendering film.

    Sets ``Book.cover_key`` to ``covers/{book_id}`` (a real shelf cover) in addition
    to seeding page 1's image, so the library shelf renders a designed/HD cover.
    """
    from app.api.security import hash_password
    from app.core.config import get_settings
    from app.db.models.enums import BookStatus, EntityType, ShotStatus
    from app.db.repositories.beat import BeatRepo
    from app.db.repositories.book import BookRepo, PageRepo
    from app.db.repositories.entity import EntityRepo
    from app.db.repositories.scene import SceneRepo
    from app.db.repositories.shot import ShotRepo, SourceSpanRepo
    from app.db.repositories.user import UserRepo
    from app.db.session import get_session
    from app.memory.cache_service import CacheService
    from app.redis.client import RedisClient
    from app.storage.object_store import ObjectStore, keys

    settings = get_settings()
    store = ObjectStore.from_settings(settings)
    redis = RedisClient.from_url(settings.redis_url)
    with contextlib.suppress(Exception):
        store.ensure_bucket()

    rendered = extract_story_pages(epub, num_pages)
    words: list[str] = [wb["text"] for p in rendered for wb in p.word_boxes]
    total_words = len(words)
    if total_words < WORD_STEP * 2:
        return None
    cover = cover_png or make_typographic_cover(title, author)

    async with get_session() as session:
        users = UserRepo(session)
        user = await users.get_by_email(EMAIL)
        if user is None:
            user = await users.create(email=EMAIL, hashed_password=hash_password(PASSWORD))
        user_id = user.id

    async with get_session() as session:
        existing = await BookRepo(session).get(book_id)
        if existing is not None:
            await session.delete(existing)

    # The designed/HD cover: a real shelf cover (covers/{book_id}, Book.cover_key),
    # also reused as page 1's image and the locked character reference. It is not
    # promoted into the film timeline; only hosted video may become playable.
    cover_key = keys.cover(book_id)
    page_image_key = f"pages/{book_id}/1.png"
    store.put_bytes(cover_key, cover, "image/png")
    store.put_bytes(page_image_key, cover, "image/png")

    STYLE, CHAR, LOC = "style_main", "char_lead", "loc_main"  # noqa: N806

    async with get_session() as session:
        await BookRepo(session).create(
            title=title, author=author, user_id=user_id,
            source_pdf_key=keys.pdf(book_id), status=BookStatus.READY,
            num_pages=len(rendered), art_direction=art, cover_key=cover_key,
            book_id=book_id,
        )
        page_repo = PageRepo(session)
        for page in rendered:
            await page_repo.create(
                book_id=book_id, page_number=page.page_number,
                image_key=page_image_key, text=page.text, word_boxes=page.word_boxes,
            )
    store.put_bytes(keys.pdf(book_id), epub.read_bytes(), "application/epub+zip")

    ref_key = keys.ref(book_id, CHAR, "ref_front.png")
    store.put_bytes(ref_key, cover, "image/png")
    async with get_session() as session:
        entities = EntityRepo(session)
        await entities.upsert_new_version(
            book_id=book_id, entity_key=CHAR, entity_type=EntityType.CHARACTER,
            name="the protagonist", valid_from_beat=0,
            description=f"The central figure of {title}.",
            appearance={
                "description": "the protagonist",
                "reference_images": [{"key": ref_key, "pose": "front", "locked": True}],
                "locked": True,
            },
            first_appearance={"page": 1, "beat_id": f"{tag}bt0000"},
        )
        await entities.upsert_new_version(
            book_id=book_id, entity_key=LOC, entity_type=EntityType.LOCATION,
            name="the setting", valid_from_beat=0,
            description=f"The principal setting of {title}.",
        )
        await entities.upsert_new_version(
            book_id=book_id, entity_key=STYLE, entity_type=EntityType.STYLE,
            name="Art direction", valid_from_beat=0, description=art,
            style_tokens={"art_direction": art, "lens": "35mm"},
        )

    num_scenes = len(rendered)
    scene_ids = [f"{tag}sc{i:03d}" for i in range(num_scenes)]
    beat_ids: list[str] = []
    async with get_session() as session:
        scenes = SceneRepo(session)
        beats = BeatRepo(session)
        for i in range(num_scenes):
            await scenes.create(
                book_id=book_id, scene_index=i, page_start=i + 1, page_end=i + 1,
                title=f"{title} — part {i + 1}", style_entity_key=STYLE, scene_id=scene_ids[i],
            )
        beat_index = 0
        for s in range(num_scenes):
            for _ in range(2):
                bid = f"{tag}bt{beat_index:04d}"
                await beats.create(
                    book_id=book_id, scene_id=scene_ids[s], beat_index=beat_index,
                    summary=f"{title} — beat {beat_index + 1}", entities=[CHAR, LOC],
                    described_visuals=art, mood="cinematic",
                    source_span={"page": s + 1, "word_range": [s * 30, s * 30 + 29]},
                    beat_id=bid,
                )
                beat_ids.append(bid)
                beat_index += 1

    # Bounded shot grid: at most ``max_shots`` shots, contiguous spans covering
    # every word (coarser than 1-shot-per-12-words, but the index stays complete).
    num_shots = min(max_shots, max(2, (total_words + WORD_STEP - 1) // WORD_STEP))
    step = max(WORD_STEP, (total_words + num_shots - 1) // num_shots)

    spans: list[dict[str, Any]] = []
    async with get_session() as session:
        shot_repo = ShotRepo(session)
        for i in range(num_shots):
            sid = f"{tag}sh{i:04d}"
            start = i * step
            if start > total_words - 1:
                break
            end = min(total_words - 1, start + step - 1)
            page_no = min(len(rendered), 1 + (i // max(1, num_shots // len(rendered))))
            beat_id = beat_ids[i % len(beat_ids)]
            scene_id = scene_ids[i % len(scene_ids)]
            snippet = " ".join(words[start:end + 1])[:160]
            source_span = {"page": page_no, "para": 1, "word_range": [start, end]}
            prompt = f"{art}. {snippet}"
            await shot_repo.create(
                id=sid, book_id=book_id, scene_id=scene_id, beat_id=beat_id,
                source_span=source_span, status=ShotStatus.PLANNED,
                render_mode="reference_to_video", prompt=prompt,
                negative_prompt="extra fingers, warped face, modern objects",
                seed=88123 + i,
                reference_set_hash=CacheService.reference_set_hash([f"{STYLE}@v1"]),
                reference_image_ids=[f"{STYLE}@v1"], duration_s=SHOT_DURATION_S,
                canon_version_at_render=1,
            )
            spans.append({
                "book_id": book_id, "word_index_start": start, "word_index_end": end,
                "shot_id": sid, "scene_id": scene_id, "beat_id": beat_id,
            })
        await SourceSpanRepo(session).bulk_insert(spans)

    owner_key = f"kinora:user:{user_id}:books"
    await redis.raw.sadd(owner_key, book_id)
    await redis.set_json(f"kinora:book:progress:{book_id}", {"stage": "ready", "pct": 1.0})
    await redis.close()

    return {"book_id": book_id, "title": title, "pages": len(rendered),
            "words": total_words, "shots": len(spans), "cover_source": cover_source}


async def amain() -> int:
    from app.core.config import get_settings
    from app.core.logging import configure_logging

    configure_logging(get_settings().log_level)
    out: list[dict[str, Any]] = []
    for gid, title, author, art, pages in BOOKS:
        epub = download(gid)
        if not epub:
            print(f"SKIP {title}: download failed", flush=True)
            continue
        try:
            res = await build_book(book_id=book_id_for(gid), tag=f"pd{gid}",
                                   title=title, author=author, art=art, epub=epub,
                                   num_pages=pages)
            if res:
                out.append(res)
                print(f"  OK {res['title']} pages={res['pages']} shots={res['shots']}", flush=True)
        except Exception as e:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            print(f"  FAIL {title}: {e}", flush=True)

    print(f"\n=== DIRECT SEED: {len(out)} books ===", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(amain()))
