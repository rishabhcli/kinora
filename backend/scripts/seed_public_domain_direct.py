#!/usr/bin/env python3
"""Build several **public-domain books DIRECTLY** through the real repositories
(exactly like :mod:`scripts.seed_e2e`) — each ``status=ready`` with real pages +
per-word boxes, a populated ``source_span_index``, a shot grid, and one playable
Ken-Burns clip — in a couple of seconds each and with **zero model spend**.

This sidesteps the slow, 429-prone *real* ingest (which stalls full novels at the
canon stage), so the desktop "Read Live · Public Domain" shelf is reliably
populated and every book is scroll-session drivable the moment it appears.

    backend/.venv/bin/python backend/scripts/seed_public_domain_direct.py

The books land in the demo user's library (``demo@kinora.local``). Re-running is
idempotent: each book has a fixed id that is wiped and rebuilt.
"""
from __future__ import annotations

import asyncio
import io
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any

import httpx

# Reuse the proven PyMuPDF RenderedPage model. fitz opens EPUB directly (reflows
# into pages), so per-word extraction works the same on Gutenberg EPUBs.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from seed_e2e import RenderedPage  # noqa: E402

_REPO = Path(__file__).resolve().parents[2]
DEST = _REPO / "assets" / "books" / "public-domain"
EMAIL = "demo@kinora.local"
PASSWORD = "demo-password-123"

WORD_STEP = 12          # words per shot (the §4.2 source-span grid step)
SHOT_DURATION_S = 5.0

# (gutenberg_id, title, author, art_direction, pages_to_rasterise)
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


def book_id_for(gid: int) -> str:
    """A stable 32-char book id per Gutenberg work (matches the repo's id width)."""
    return f"pubdom{gid}".ljust(32, "0")[:32]


def download(gid: int) -> Path | None:
    DEST.mkdir(parents=True, exist_ok=True)
    out = DEST / f"pg{gid}.epub"
    if out.exists() and out.stat().st_size > 20000:
        return out
    for url in (
        f"https://www.gutenberg.org/cache/epub/{gid}/pg{gid}.epub",
        f"https://www.gutenberg.org/ebooks/{gid}.epub3.images",
        f"https://www.gutenberg.org/ebooks/{gid}.epub.images",
    ):
        try:
            with httpx.Client(follow_redirects=True, timeout=180.0,
                              headers={"User-Agent": "Mozilla/5.0 (Kinora seed)"}) as dc:
                r = dc.get(url)
                r.raise_for_status()
            if len(r.content) < 20000:
                continue
            out.write_bytes(r.content)
            print(f"  downloaded {url} ({len(r.content) // 1024} KiB)", flush=True)
            return out
        except Exception as e:  # noqa: BLE001
            print(f"  dl fail {url}: {e}", flush=True)
    return None


# --------------------------------------------------------------------------- #
# Story extraction (skip Gutenberg front matter) + a designed typographic cover
# --------------------------------------------------------------------------- #

_FRONT_MATTER = (
    "project gutenberg", "gutenberg-tm", "ebook is for the use", "this ebook",
    "start of the project", "produced by", "character set encoding", "*** start",
)


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
            # No raster needed: only page 1's image is used (the cover), and the
            # reading room renders page TEXT. png stays empty.
            pages.append(RenderedPage(page_number=offset + 1, png=b"",
                                      text=" ".join(parts), word_boxes=boxes))
        return pages
    finally:
        doc.close()


# Mood-matched cover gradients (accent top → dark bottom) per Gutenberg id.
_COVER_PALETTES: dict[int, tuple[tuple[int, int, int], tuple[int, int, int]]] = {
    1952: ((120, 104, 52), (22, 19, 8)),    # The Yellow Wallpaper — sickly gold-green
    5200: ((58, 70, 86), (12, 16, 22)),     # The Metamorphosis — cold grey-blue
    43:   ((40, 60, 64), (8, 13, 14)),       # Jekyll & Hyde — noir teal
    46:   ((112, 44, 38), (24, 10, 8)),      # A Christmas Carol — warm crimson
    11:   ((78, 60, 112), (16, 11, 28)),     # Alice — whimsical violet
}
_CREAM = (242, 234, 216)
_GOLD = (212, 164, 78)
_FONT_PATHS = (
    "/System/Library/Fonts/Supplemental/Georgia.ttf",
    "/System/Library/Fonts/Supplemental/Times New Roman.ttf",
    "/Library/Fonts/Georgia.ttf",
    "/System/Library/Fonts/Supplemental/Baskerville.ttc",
    "/System/Library/Fonts/Supplemental/Hoefler Text.ttc",
)


@lru_cache(maxsize=24)
def _font(size: int):
    from PIL import ImageFont
    for p in _FONT_PATHS:
        try:
            return ImageFont.truetype(p, size)
        except Exception:  # noqa: BLE001
            continue
    return ImageFont.load_default()


def _wrap(draw, text: str, font, max_w: float) -> list[str]:
    lines: list[str] = []
    cur = ""
    for word in text.split():
        trial = (cur + " " + word).strip()
        if not cur or draw.textlength(trial, font=font) <= max_w:
            cur = trial
        else:
            lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return lines


def make_cover_png(title: str, author: str, gid: int, W: int = 720, H: int = 1080) -> bytes:
    """A clean, designed book cover (gradient + serif title) — these EPUBs ship
    no art, so a plain page scan would look like a document on the shelf."""
    from PIL import Image, ImageDraw

    top, bot = _COVER_PALETTES.get(gid, ((54, 60, 72), (12, 14, 20)))
    img = Image.new("RGB", (W, H), bot)
    draw = ImageDraw.Draw(img, "RGBA")
    for y in range(H):  # vertical gradient, accent weighted to the upper third
        f = (y / (H - 1)) ** 1.15
        col = (int(top[0] + (bot[0] - top[0]) * f),
               int(top[1] + (bot[1] - top[1]) * f),
               int(top[2] + (bot[2] - top[2]) * f))
        draw.line([(0, y), (W, y)], fill=col)
    m = 46
    draw.rectangle([m, m, W - m, H - m], outline=(*_GOLD, 90), width=2)

    tf = _font(74)
    lines = _wrap(draw, title, tf, W - 2 * m - 56)
    while len(lines) > 4 and tf.size > 40:
        tf = _font(tf.size - 6)
        lines = _wrap(draw, title, tf, W - 2 * m - 56)
    y = int(H * 0.30)
    for ln in lines:
        w = draw.textlength(ln, font=tf)
        draw.text(((W - w) / 2, y), ln, font=tf, fill=_CREAM)
        y += int(tf.size * 1.16)
    ry = y + 24
    draw.line([(W / 2 - 70, ry), (W / 2 + 70, ry)], fill=_GOLD, width=2)
    af = _font(34)
    aw = draw.textlength(author, font=af)
    draw.text(((W - aw) / 2, ry + 28), author, font=af, fill=(*_CREAM, 220))

    out = io.BytesIO()
    img.save(out, "PNG")
    return out.getvalue()


async def build_book(*, book_id: str, tag: str, gid: int, title: str, author: str,
                     art: str, epub: Path, num_pages: int) -> dict[str, Any] | None:
    """Write one ready, span-indexed, playable book directly (no model spend)."""
    from app.api.security import hash_password
    from app.core.config import get_settings
    from app.db.hashing import compute_shot_hash
    from app.db.models.enums import BookStatus, EntityType, ShotStatus
    from app.db.repositories.beat import BeatRepo
    from app.db.repositories.book import BookRepo, PageRepo
    from app.db.repositories.entity import EntityRepo
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
    try:
        store.ensure_bucket()
    except Exception:  # noqa: BLE001
        pass

    rendered = extract_story_pages(epub, num_pages)
    words: list[str] = [wb["text"] for p in rendered for wb in p.word_boxes]
    # A designed typographic cover (these text EPUBs carry no art) — reused as the
    # shelf cover, the film poster, the Ken-Burns source, and the locked char ref.
    cover_png = make_cover_png(title, author, gid)
    total_words = len(words)
    if total_words < WORD_STEP * 2:
        print(f"  too few words ({total_words}); skipping {title}", flush=True)
        return None

    # ----- user (demo, real bcrypt path) ----------------------------------- #
    async with get_session() as session:
        users = UserRepo(session)
        user = await users.get_by_email(EMAIL)
        if user is None:
            user = await users.create(email=EMAIL, hashed_password=hash_password(PASSWORD))
        user_id = user.id

    # ----- wipe any prior build of THIS book (cascade) --------------------- #
    async with get_session() as session:
        existing = await BookRepo(session).get(book_id)
        if existing is not None:
            await session.delete(existing)

    # Store the cover once; every page's image points at it. The reading room
    # renders page TEXT, and only page 1's image is consumed (cover + poster).
    cover_key = f"pages/{book_id}/1.png"
    store.put_bytes(cover_key, cover_png, "image/png")

    STYLE, CHAR, LOC = "style_main", "char_lead", "loc_main"

    # ----- book + pages ---------------------------------------------------- #
    async with get_session() as session:
        await BookRepo(session).create(
            title=title, author=author, user_id=user_id,
            source_pdf_key=keys.pdf(book_id), status=BookStatus.READY,
            num_pages=len(rendered), art_direction=art, book_id=book_id,
        )
        page_repo = PageRepo(session)
        for page in rendered:
            await page_repo.create(
                book_id=book_id, page_number=page.page_number,
                image_key=cover_key, text=page.text, word_boxes=page.word_boxes,
            )
    store.put_bytes(keys.pdf(book_id), epub.read_bytes(), "application/epub+zip")

    # ----- canon: a Style node (drives the look) + a lead char + location -- #
    ref_key = keys.ref(book_id, CHAR, "ref_front.png")
    store.put_bytes(ref_key, cover_png, "image/png")
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

    # ----- scenes (one per page) + beats ----------------------------------- #
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

    # ----- one accepted shot with a real, playable vertical Ken-Burns clip -- #
    clip_bytes = ken_burns_over_image(cover_png, duration_s=2.5, size=(540, 960), fps=24)
    accepted = f"{tag}sh0000"
    clip_key = keys.clip(book_id, accepted)
    store.put_bytes(clip_key, clip_bytes, "video/mp4")
    last_frame_key = keys.lastframe(book_id, accepted)
    store.put_bytes(last_frame_key, cover_png, "image/png")

    num_shots = max(2, (total_words + WORD_STEP - 1) // WORD_STEP)
    accepted_refs = [f"{CHAR}@v1", f"{STYLE}@v1"]
    ref_hash = CacheService.reference_set_hash(accepted_refs)

    # ----- shot grid + source-span index (covers every word) --------------- #
    spans: list[dict[str, Any]] = []
    async with get_session() as session:
        shot_repo = ShotRepo(session)
        cache = ShotCacheRepo(session)
        for i in range(num_shots):
            sid = f"{tag}sh{i:04d}"
            start = i * WORD_STEP
            end = min(total_words - 1, start + WORD_STEP - 1)
            page_no = min(len(rendered), 1 + (i // max(1, num_shots // len(rendered))))
            beat_id = beat_ids[i % len(beat_ids)]
            scene_id = scene_ids[i % len(scene_ids)]
            snippet = " ".join(words[start:end + 1])[:160]
            source_span = {"page": page_no, "para": 1, "word_range": [start, end]}
            prompt = f"{art}. {snippet}"
            if i == 0:
                shot_hash = compute_shot_hash(
                    book_id=book_id, beat_id=beat_id, canon_version_at_render=1,
                    render_mode="ken_burns_keyframe", seed=88123, reference_set_hash=ref_hash,
                )
                await shot_repo.create(
                    id=sid, book_id=book_id, scene_id=scene_id, beat_id=beat_id,
                    source_span=source_span, status=ShotStatus.ACCEPTED,
                    render_mode="ken_burns_keyframe", prompt=prompt,
                    negative_prompt="extra fingers, warped face, modern objects", seed=88123,
                    reference_set_hash=ref_hash, reference_image_ids=accepted_refs,
                    duration_s=SHOT_DURATION_S,
                    output={"clip_key": clip_key, "last_frame_key": last_frame_key},
                    narration={"text": snippet, "word_timestamps": []},
                    qa={"ccs": 0.92, "style_drift": 0.04, "timeline_ok": True,
                        "motion_artifact": 0.08, "score": 0.9, "verdict": "pass",
                        "reason": "seed"},
                    cost={"video_seconds": 0.0, "tokens": 0},
                    shot_hash=shot_hash, canon_version_at_render=1,
                )
                await cache.put(
                    shot_hash=shot_hash, book_id=book_id, clip_key=clip_key,
                    last_frame_key=last_frame_key, qa={"verdict": "pass", "ccs": 0.92},
                    video_seconds=0.0,
                )
            else:
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

    # ----- ownership (Redis) — ADD only (never wipe the user's set) -------- #
    owner_key = f"kinora:user:{user_id}:books"
    await redis.raw.sadd(owner_key, book_id)
    await redis.set_json(f"kinora:book:progress:{book_id}", {"stage": "ready", "pct": 1.0})
    await redis.close()

    return {"book_id": book_id, "title": title, "pages": len(rendered),
            "words": total_words, "shots": num_shots, "spans": len(spans)}


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
        print(f"building {title} from {epub.name} ({epub.stat().st_size // 1024} KiB)…", flush=True)
        try:
            res = await build_book(book_id=book_id_for(gid), tag=f"pd{gid}", gid=gid,
                                   title=title, author=author, art=art, epub=epub,
                                   num_pages=pages)
            if res:
                out.append(res)
                print(f"  OK {res}", flush=True)
        except Exception as e:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            print(f"  FAIL {title}: {e}", flush=True)

    print("\n=== DIRECT SEED SUMMARY ===", flush=True)
    for r in out:
        print(f"  ready  {r['title']:34s} pages={r['pages']} words={r['words']} shots={r['shots']}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(amain()))
