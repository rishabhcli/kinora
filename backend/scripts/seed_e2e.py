#!/usr/bin/env python3
"""Seed a small, deterministic, PRE-INGESTED book for the Playwright e2e suite.

Unlike :mod:`scripts.seed_demo` (which drives the *real* Phase-A ingest and
therefore needs DashScope + minutes of wall-clock for the VL pass), this script
writes a complete, ready-to-watch book **directly through the real repositories
and object store** in a couple of seconds and with **zero model spend**
(``KINORA_LIVE_VIDEO`` stays off; no Wan / Qwen / CosyVoice calls). That makes
the e2e fast and fully deterministic while still exercising the real schema, the
real source-span index, the real degradation-ladder renderer, and the real
buffer-trace endpoint.

What it creates (idempotent — a fixed ``book_id`` is wiped and rebuilt each run):

* a **user** ``e2e@kinora.test`` (bcrypt-hashed, the real auth path) that owns
  the book (ownership tracked in Redis exactly like the upload route);
* a **book** (``status=ready``) with ``num_pages`` set;
* **2-3 real pages** rasterised from the bundled public-domain demo PDF
  (``assets/books/the_frog_king.pdf``) via PyMuPDF, uploaded to the object
  store, with the extracted text + normalised per-word boxes (the karaoke layer
  + scroll focus consume these);
* **canon entities** — two characters with *locked* reference images, a location,
  and a Style node with style tokens (the surgical canon-edit target);
* a few **scenes** and **beats**;
* a grid of **shots** with ``source_span`` + a populated **source_span_index**
  (the §4.2 map the Scheduler/buffer-trace read), most ``planned`` so the
  buffer-trace sawtooth has shots to promote, plus a few keyframed ones;
* at least one **accepted shot with a real, playable clip** — a tiny Ken-Burns
  mp4 produced by the real ``app.render.degrade`` ladder over a page still and
  uploaded to the object store, with a ``shot_cache`` row (so the workspace has
  playable content with no video spend);
* an **eval report** cached in Redis (the crew-vs-baseline §13 numbers the
  metrics panel renders).

The e2e fixtures import the same constants (mirrored in
``frontend/e2e/fixtures/seed.ts``): the login lands on the shelf, the seeded
book is found by title, and the canon-edit targets ``style_storybook``.

Run from the backend venv against the configured infra (env / ``backend/.env``):

    backend/.venv/bin/python backend/scripts/seed_e2e.py

Configuration is the usual :class:`app.core.config.Settings`
(``DATABASE_URL`` / ``REDIS_URL`` / ``S3_*``); ``DASHSCOPE_API_KEY`` only needs
to be set (any value) so settings validate — no model is ever called.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# --------------------------------------------------------------------------- #
# Shared, deterministic constants (mirrored in frontend/e2e/fixtures/seed.ts)
# --------------------------------------------------------------------------- #

E2E_EMAIL = "e2e@kinora.test"
E2E_PASSWORD = "e2e-password-123"

#: A fixed book id so re-running wipes and rebuilds exactly one seeded book.
BOOK_ID = "e2e0frog0king00000000000000seed1"
BOOK_TITLE = "The Frog-King (e2e seed)"
BOOK_AUTHOR = "Brothers Grimm (public domain)"
ART_DIRECTION = "painterly storybook"

#: The Style canon node the director-mode canon-edit targets. It carries
#: ``style_tokens`` (no appearance/reference image), so ``canon.upsert_entity``
#: never tries to embed a locked reference — the edit runs with no model spend.
STYLE_ENTITY_KEY = "style_storybook"
CHAR_FROG_KEY = "char_frog_prince"
CHAR_PRINCESS_KEY = "char_princess"
LOC_WELL_KEY = "loc_well"

#: Source-span grid: ``shot i`` starts at ``i * WORD_STEP`` (the §4.2 sorted map).
WORD_STEP = 10
SHOT_DURATION_S = 5.0
#: Shots cover ~ N*WORD_STEP words — enough that the default 180 s buffer-trace
#: at v≈4 wps (≈720 words) never runs out and forms a full sawtooth.
DEFAULT_NUM_SHOTS = 96
#: How many demo-PDF story pages to rasterise (skipping the title page).
DEFAULT_NUM_PAGES = 3

_REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PDF = _REPO_ROOT / "assets" / "books" / "the_frog_king.pdf"


# --------------------------------------------------------------------------- #
# Result
# --------------------------------------------------------------------------- #


@dataclass
class SeedResult:
    """A summary of what was written (printed at the end)."""

    book_id: str
    user_id: str
    email: str
    title: str
    num_pages: int
    num_words: int
    num_scenes: int
    num_beats: int
    num_shots: int
    num_accepted: int
    num_spans: int
    num_entities: int
    clip_keys: list[str] = field(default_factory=list)
    keyframe_keys: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "book_id": self.book_id,
            "user_id": self.user_id,
            "email": self.email,
            "title": self.title,
            "num_pages": self.num_pages,
            "num_words": self.num_words,
            "num_scenes": self.num_scenes,
            "num_beats": self.num_beats,
            "num_shots": self.num_shots,
            "num_accepted_shots": self.num_accepted,
            "num_spans": self.num_spans,
            "num_entities": self.num_entities,
            "clip_keys": self.clip_keys,
            "keyframe_keys": self.keyframe_keys,
        }


# --------------------------------------------------------------------------- #
# PDF rasterisation + word-box extraction (real PyMuPDF, no model)
# --------------------------------------------------------------------------- #


@dataclass
class RenderedPage:
    page_number: int
    png: bytes
    text: str
    word_boxes: list[dict[str, Any]]


def render_pages(pdf_path: Path, num_pages: int, *, dpi: int = 110) -> list[RenderedPage]:
    """Rasterise the first ``num_pages`` *story* pages and extract per-word boxes.

    The demo PDF's page 0 is a title page; the story (with characters) starts at
    page index 1, so we render story pages into book pages 1..N. Word boxes are
    normalised to ``[0, 1]`` page coordinates (``[x, y, w, h]``) and carry a
    global ``word_index`` so they tie into the source-span index (§9.4).
    """
    import fitz  # PyMuPDF

    doc = fitz.open(str(pdf_path))
    try:
        # Prefer the story pages (skip the title page when the book is large
        # enough); fall back to from-the-start for an unexpectedly short PDF.
        start = 1 if doc.page_count > num_pages else 0
        pages: list[RenderedPage] = []
        global_word = 0
        for offset in range(num_pages):
            src_index = min(start + offset, doc.page_count - 1)
            page = doc[src_index]
            rect = page.rect
            width = float(rect.width) or 1.0
            height = float(rect.height) or 1.0
            pix = page.get_pixmap(dpi=dpi)
            png: bytes = pix.tobytes("png")

            word_boxes: list[dict[str, Any]] = []
            text_parts: list[str] = []
            for word in page.get_text("words"):
                x0, y0, x1, y1, text = word[0], word[1], word[2], word[3], word[4]
                if not text.strip():
                    continue
                word_boxes.append(
                    {
                        "word_index": global_word,
                        "text": text,
                        "bbox": [
                            round(x0 / width, 6),
                            round(y0 / height, 6),
                            round(max(0.0, x1 - x0) / width, 6),
                            round(max(0.0, y1 - y0) / height, 6),
                        ],
                    }
                )
                text_parts.append(text)
                global_word += 1
            pages.append(
                RenderedPage(
                    page_number=offset + 1,
                    png=png,
                    text=" ".join(text_parts),
                    word_boxes=word_boxes,
                )
            )
        return pages
    finally:
        doc.close()


# --------------------------------------------------------------------------- #
# The seed
# --------------------------------------------------------------------------- #


async def seed_e2e(
    *, pdf_path: Path, num_pages: int = DEFAULT_NUM_PAGES, num_shots: int = DEFAULT_NUM_SHOTS
) -> SeedResult:
    """Write the full deterministic e2e book and return a summary."""
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

    # Idempotent bucket create so a fresh MinIO/OSS works out of the box.
    try:
        store.ensure_bucket()
    except Exception as exc:  # noqa: BLE001 - bucket may be pre-provisioned/read-only
        print(f"warning: ensure_bucket failed ({exc}); continuing", file=sys.stderr)

    rendered = render_pages(pdf_path, num_pages)
    total_words = sum(len(p.word_boxes) for p in rendered)

    # ----- user (get-or-create, real bcrypt path) -------------------------- #
    async with get_session() as session:
        users = UserRepo(session)
        user = await users.get_by_email(E2E_EMAIL)
        if user is None:
            user = await users.create(
                email=E2E_EMAIL, hashed_password=hash_password(E2E_PASSWORD)
            )
        user_id = user.id

    # ----- wipe any prior seeded book (cascade), then rebuild -------------- #
    async with get_session() as session:
        existing = await BookRepo(session).get(BOOK_ID)
        if existing is not None:
            await session.delete(existing)

    # Upload page stills first (so we can reuse page 1 for the Ken-Burns clip).
    page_image_keys: dict[int, str] = {}
    for page in rendered:
        key = f"pages/{BOOK_ID}/{page.page_number}.png"
        store.put_bytes(key, page.png, "image/png")
        page_image_keys[page.page_number] = key

    # ----- book + pages ---------------------------------------------------- #
    async with get_session() as session:
        await BookRepo(session).create(
            title=BOOK_TITLE,
            author=BOOK_AUTHOR,
            source_pdf_key=keys.pdf(BOOK_ID),
            status=BookStatus.READY,
            num_pages=len(rendered),
            art_direction=ART_DIRECTION,
            book_id=BOOK_ID,
        )
        page_repo = PageRepo(session)
        for page in rendered:
            await page_repo.create(
                book_id=BOOK_ID,
                page_number=page.page_number,
                image_key=page_image_keys[page.page_number],
                text=page.text,
                word_boxes=page.word_boxes,
            )

    # Also keep the source PDF in object storage (parity with the upload route).
    store.put_bytes(keys.pdf(BOOK_ID), pdf_path.read_bytes(), "application/pdf")

    # ----- canon entities (2 chars w/ locked refs, a location, a Style node) #
    char_refs: dict[str, str] = {}
    for key in (CHAR_FROG_KEY, CHAR_PRINCESS_KEY):
        ref_key = keys.ref(BOOK_ID, key, "ref_front.png")
        # Reuse a page still as a real, locked reference image (no model spend).
        store.put_bytes(ref_key, rendered[0].png, "image/png")
        char_refs[key] = ref_key

    async with get_session() as session:
        entities = EntityRepo(session)
        await entities.upsert_new_version(
            book_id=BOOK_ID,
            entity_key=CHAR_FROG_KEY,
            entity_type=EntityType.CHARACTER,
            name="the Frog Prince",
            valid_from_beat=0,
            aliases=["the Frog", "the Prince"],
            description="A frog under an enchantment who becomes a kind young prince.",
            appearance={
                "description": "green frog with golden eyes; later a prince in a crimson cloak",
                "reference_images": [
                    {"key": char_refs[CHAR_FROG_KEY], "pose": "front", "locked": True}
                ],
                "locked": True,
            },
            first_appearance={"page": 2, "beat_id": "beat_0001"},
        )
        await entities.upsert_new_version(
            book_id=BOOK_ID,
            entity_key=CHAR_PRINCESS_KEY,
            entity_type=EntityType.CHARACTER,
            name="the Princess",
            valid_from_beat=0,
            aliases=["the King's daughter"],
            description="The King's youngest daughter, radiant, with a golden ball.",
            appearance={
                "description": "young princess in a red gown with a small golden crown",
                "reference_images": [
                    {"key": char_refs[CHAR_PRINCESS_KEY], "pose": "front", "locked": True}
                ],
                "locked": True,
            },
            first_appearance={"page": 1, "beat_id": "beat_0000"},
        )
        await entities.upsert_new_version(
            book_id=BOOK_ID,
            entity_key=LOC_WELL_KEY,
            entity_type=EntityType.LOCATION,
            name="the old well",
            valid_from_beat=0,
            description="A deep stone well beneath an old lime-tree in the dark forest.",
        )
        # The Style node — no appearance/reference, so a canon-edit on it never
        # touches the embedder (zero model spend), yet it drives every shot's look.
        await entities.upsert_new_version(
            book_id=BOOK_ID,
            entity_key=STYLE_ENTITY_KEY,
            entity_type=EntityType.STYLE,
            name="Painterly storybook",
            valid_from_beat=0,
            description="Warm painterly storybook look.",
            style_tokens={
                "palette": "warm gold and forest green",
                "lens": "35mm",
                "art_direction": "painterly storybook",
            },
        )
    num_entities = 4

    # ----- scenes + beats -------------------------------------------------- #
    scene_titles = ["The golden ball", "A promise at the well", "The spell is broken"]
    num_scenes = min(len(scene_titles), max(1, len(rendered)))
    scene_ids: list[str] = [f"scene_{i:03d}" for i in range(num_scenes)]
    beat_ids: list[str] = []
    async with get_session() as session:
        scenes = SceneRepo(session)
        beats = BeatRepo(session)
        for i in range(num_scenes):
            page_no = min(i + 1, len(rendered))
            await scenes.create(
                book_id=BOOK_ID,
                scene_index=i,
                page_start=page_no,
                page_end=page_no,
                title=scene_titles[i],
                style_entity_key=STYLE_ENTITY_KEY,
                scene_id=scene_ids[i],
            )
        # A few beats per scene (the §4.2 planning atoms).
        beats_per_scene = 3
        beat_index = 0
        for s in range(num_scenes):
            for b in range(beats_per_scene):
                beat_id = f"beat_{beat_index:04d}"
                await beats.create(
                    book_id=BOOK_ID,
                    scene_id=scene_ids[s],
                    beat_index=beat_index,
                    summary=f"{scene_titles[s]} — beat {b + 1}",
                    entities=[CHAR_FROG_KEY, CHAR_PRINCESS_KEY, LOC_WELL_KEY],
                    described_visuals="painterly storybook scene at the old well",
                    mood="wonder",
                    source_span={
                        "page": min(s + 1, len(rendered)),
                        "word_range": [s * 30, s * 30 + 29],
                    },
                    beat_id=beat_id,
                )
                beat_ids.append(beat_id)
                beat_index += 1

    # ----- the accepted shot's real Ken-Burns clip (degradation ladder) ---- #
    # A tiny, real, playable mp4 over a page still — zero video-seconds spent.
    clip_bytes = ken_burns_over_image(
        rendered[0].png, duration_s=2.0, size=(320, 180), fps=24
    )
    accepted_shot_id = "shot_0000"
    clip_key = keys.clip(BOOK_ID, accepted_shot_id)
    store.put_bytes(clip_key, clip_bytes, "video/mp4")
    last_frame_key = keys.lastframe(BOOK_ID, accepted_shot_id)
    store.put_bytes(last_frame_key, rendered[0].png, "image/png")

    # A couple of speculative keyframes (cheap stills) for early beats so the
    # shot timeline shows thumbnails and the Ken-Burns bridge has an image.
    keyframe_keys: list[str] = []
    for bi in beat_ids[:2]:
        kkey = keys.keyframe(BOOK_ID, bi)
        store.put_bytes(kkey, rendered[0].png, "image/png")
        keyframe_keys.append(kkey)

    # Only a small "dependent set" of shots references the Style node, so a
    # director canon-edit on the Style entity regenerates just those (a surgical
    # re-render, §8.7) — not the whole book. Every shot still references the
    # character (the canon completeness the task asks for).
    style_dependent_shots = 3
    accepted_refs = [f"{CHAR_FROG_KEY}@v1", f"{STYLE_ENTITY_KEY}@v1"]
    ref_set_hash = CacheService.reference_set_hash(accepted_refs)

    def _refs_for(i: int) -> list[str]:
        if i < style_dependent_shots:
            return [f"{CHAR_FROG_KEY}@v1", f"{STYLE_ENTITY_KEY}@v1"]
        return [f"{CHAR_FROG_KEY}@v1"]

    # ----- shots + source-span index --------------------------------------- #
    num_accepted = 0
    spans: list[dict[str, Any]] = []
    async with get_session() as session:
        shot_repo = ShotRepo(session)
        cache = ShotCacheRepo(session)
        for i in range(num_shots):
            shot_id = f"shot_{i:04d}"
            start = i * WORD_STEP
            end = start + WORD_STEP - 1
            page_no = min(len(rendered), 1 + (i // max(1, num_shots // len(rendered))))
            beat_id = beat_ids[i % len(beat_ids)]
            scene_id = scene_ids[i % len(scene_ids)]
            source_span = {"page": page_no, "para": 1, "word_range": [start, end]}

            if i == 0:
                # The one accepted shot with real playable content (a Ken-Burns clip).
                shot_hash = compute_shot_hash(
                    book_id=BOOK_ID,
                    beat_id=beat_id,
                    canon_version_at_render=1,
                    render_mode="ken_burns_keyframe",
                    seed=88123,
                    reference_set_hash=ref_set_hash,
                )
                await shot_repo.create(
                    id=shot_id,
                    book_id=BOOK_ID,
                    scene_id=scene_id,
                    beat_id=beat_id,
                    source_span=source_span,
                    status=ShotStatus.ACCEPTED,
                    render_mode="ken_burns_keyframe",
                    prompt="The Princess by the old well, golden ball in hand; slow push-in.",
                    negative_prompt="extra fingers, warped face, modern objects",
                    seed=88123,
                    reference_set_hash=ref_set_hash,
                    reference_image_ids=accepted_refs,
                    duration_s=SHOT_DURATION_S,
                    output={"clip_key": clip_key, "last_frame_key": last_frame_key},
                    narration={"text": rendered[0].text[:240], "word_timestamps": []},
                    qa={
                        "ccs": 0.92,
                        "style_drift": 0.04,
                        "timeline_ok": True,
                        "motion_artifact": 0.08,
                        "score": 0.9,
                        "verdict": "pass",
                        "reason": "identity locked; palette coherent",
                    },
                    cost={"video_seconds": 0.0, "tokens": 1840},
                    shot_hash=shot_hash,
                    canon_version_at_render=1,
                )
                await cache.put(
                    shot_hash=shot_hash,
                    book_id=BOOK_ID,
                    clip_key=clip_key,
                    last_frame_key=last_frame_key,
                    qa={"verdict": "pass", "ccs": 0.92},
                    video_seconds=0.0,
                )
                num_accepted += 1
            else:
                # Uncommitted shots: a couple keyframed near the front, the rest
                # planned — so the buffer-trace has shots to promote (the sawtooth).
                status = ShotStatus.KEYFRAMED if i <= 2 else ShotStatus.PLANNED
                await shot_repo.create(
                    id=shot_id,
                    book_id=BOOK_ID,
                    scene_id=scene_id,
                    beat_id=beat_id,
                    source_span=source_span,
                    status=status,
                    render_mode="reference_to_video",
                    prompt=f"Beat {beat_id}: painterly storybook shot.",
                    negative_prompt="extra fingers, warped face, modern objects",
                    seed=88123 + i,
                    reference_set_hash=CacheService.reference_set_hash(_refs_for(i)),
                    reference_image_ids=_refs_for(i),
                    duration_s=SHOT_DURATION_S,
                    canon_version_at_render=1,
                )
            spans.append(
                {
                    "book_id": BOOK_ID,
                    "word_index_start": start,
                    "word_index_end": end,
                    "shot_id": shot_id,
                    "scene_id": scene_id,
                    "beat_id": beat_id,
                }
            )
        await SourceSpanRepo(session).bulk_insert(spans)

    # ----- ownership (Redis) + cached eval report -------------------------- #
    owner_key = f"kinora:user:{user_id}:books"
    await redis.delete(owner_key)
    await redis.raw.sadd(owner_key, BOOK_ID)
    await redis.set_json(
        f"kinora:book:progress:{BOOK_ID}", {"stage": "ready", "pct": 1.0}
    )
    await redis.set_json(
        f"kinora:eval:report:{BOOK_ID}",
        {
            "book_id": BOOK_ID,
            "ccs": {"crew": 0.91, "baseline": 0.78},
            "efficiency": {"crew": 86.0, "baseline": 61.0},
            "regen_rate": {"crew": 0.12, "baseline": 0.34},
            "style_drift": {"crew": 0.04, "baseline": 0.11},
            "runs": 3,
            "thresholds": {"ccs": 0.85, "style_drift": 0.08, "motion": 0.25},
            "per_character_ccs": {CHAR_FROG_KEY: 0.92, CHAR_PRINCESS_KEY: 0.90},
        },
    )
    await redis.close()

    return SeedResult(
        book_id=BOOK_ID,
        user_id=user_id,
        email=E2E_EMAIL,
        title=BOOK_TITLE,
        num_pages=len(rendered),
        num_words=total_words,
        num_scenes=num_scenes,
        num_beats=len(beat_ids),
        num_shots=num_shots,
        num_accepted=num_accepted,
        num_spans=len(spans),
        num_entities=num_entities,
        clip_keys=[clip_key],
        keyframe_keys=keyframe_keys,
    )


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python backend/scripts/seed_e2e.py",
        description="Seed a small, deterministic pre-ingested book for the Playwright e2e suite.",
    )
    parser.add_argument("--pdf", type=Path, default=DEFAULT_PDF, help="demo PDF path")
    parser.add_argument(
        "--pages", type=int, default=DEFAULT_NUM_PAGES, help="story pages to rasterise"
    )
    parser.add_argument(
        "--shots", type=int, default=DEFAULT_NUM_SHOTS, help="shots in the source-span grid"
    )
    args = parser.parse_args(argv)

    pdf_path = args.pdf if args.pdf.is_absolute() else (Path.cwd() / args.pdf)
    if not pdf_path.exists():
        print(f"demo PDF not found: {pdf_path}", file=sys.stderr)
        print("build it first: python assets/books/build_demo_pdf.py", file=sys.stderr)
        return 1

    from app.core.config import get_settings
    from app.core.logging import configure_logging

    configure_logging(get_settings().log_level)
    result = asyncio.run(
        seed_e2e(pdf_path=pdf_path, num_pages=max(1, args.pages), num_shots=max(2, args.shots))
    )

    print("\n=== E2E SEED OK ===")
    for key, value in result.to_dict().items():
        print(f"  {key}: {value}")
    print("\nlogin with:")
    print(f"  email:    {E2E_EMAIL}")
    print(f"  password: {E2E_PASSWORD}")
    print(f"  title:    {BOOK_TITLE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
