#!/usr/bin/env python3
"""Seed the **100+ book public-domain library** directly — zero model spend,
idempotent (Agent 05, WS1).

Reads the frozen catalogue (``assets/books/catalog.json``, 130 canonical titles),
downloads each Gutenberg EPUB (cached), sources a **high-definition cover** (Open
Library → Google Books, up-scaled, with a designed typographic fallback), and
builds a ``ready`` book directly through the real repositories via the shared
:func:`scripts.seed_public_domain_direct.build_book` engine — pages + per-word
boxes, a bounded shot grid, one playable Ken-Burns clip, ``Book.cover_key`` set.

    backend/.venv/bin/python backend/scripts/seed_library_100.py            # full seed
    backend/.venv/bin/python backend/scripts/seed_library_100.py --limit 10 # quick subset
    backend/.venv/bin/python backend/scripts/seed_library_100.py --no-hd  # generated (fast)
    backend/.venv/bin/python backend/scripts/seed_library_100.py --force    # rebuild existing

Idempotent: a book already ``ready`` with a cover is skipped (re-runs change
nothing and are near-instant); ``--force`` rebuilds. Total DashScope spend ≈ 0.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from seed_public_domain_direct import build_book, download  # noqa: E402

from app.library import covers  # noqa: E402
from app.library.catalog import CatalogEntry, load_catalog  # noqa: E402

_REPO = Path(__file__).resolve().parents[2]
CATALOG = _REPO / "assets" / "books" / "catalog.json"
REPORT = _REPO / "assets" / "books" / "seed-report.json"
PAGES_TO_RASTERISE = 8
#: Concurrent EPUB prefetch fan-out — one book per mirror (pglaf/aleph/www) so no
#: single host sees concurrency (Gutenberg drops concurrent transfers per host).
PREFETCH_WORKERS = 3


def _prefetch(entries: list[CatalogEntry]) -> None:
    """Fetch missing EPUBs concurrently across mirrors (cached ones return fast)."""
    print(f"prefetching {len(entries)} EPUBs (×{PREFETCH_WORKERS} mirrors)…", flush=True)
    done = 0
    with ThreadPoolExecutor(max_workers=PREFETCH_WORKERS) as pool:
        futures = [
            pool.submit(download, e.gutenberg_id, e.epub_url, mirror_start=i % PREFETCH_WORKERS)
            for i, e in enumerate(entries)
        ]
        for _ in as_completed(futures):
            done += 1
            if done % 15 == 0:
                print(f"  prefetched {done}/{len(entries)}", flush=True)

#: A cinematic art-direction per genre (drives the STYLE entity + shot prompts).
_ART_BY_GENRE: dict[str, str] = {
    "Classics": "painterly classical realism, golden-hour light, fine detail",
    "Gothic & Horror": "victorian noir, gaslit fog, deep chiaroscuro, unsettling stillness",
    "Science Fiction": "retro-futurist, cool steel light, vast scale, cinematic haze",
    "Fantasy": "luminous storybook, dreamlike, richly saturated, mythic",
    "Adventure": "sweeping vistas, warm adventurous light, dynamic, epic",
    "Mystery & Detective": "moody noir, rain-slick streets, low key, amber lamplight",
    "Romance": "soft warm bokeh, tender light, intimate, painterly",
    "Children's": "whimsical illustrated, bright, playful, gentle",
    "Poetry": "lyrical impressionist, soft diffuse light, evocative",
    "Drama": "theatrical, sculpted spotlight, expressive faces, deep contrast",
    "Philosophy & Essays": "austere, contemplative, muted palette, classical light",
    "Historical": "richly textured period, candle and daylight, tapestry tones",
    "World Literature": "earthy naturalism, warm regional light, textured",
}


def art_direction_for(genre: str) -> str:
    return _ART_BY_GENRE.get(genre, _ART_BY_GENRE["Classics"])


def _cover_for(entry: CatalogEntry, *, use_hd: bool) -> tuple[bytes, str]:
    """Best HD cover bytes for a catalogue entry, else a designed fallback."""
    if use_hd and entry.author:
        sourced = covers.source_hd_cover(entry.title, entry.author)
        if sourced is not None:
            return sourced
    return covers.make_typographic_cover(entry.title, entry.author), "generated"


async def _is_seeded(book_id: str) -> bool:
    """True when the book is already ``ready`` with a cover (idempotent skip)."""
    from app.db.models.enums import BookStatus
    from app.db.repositories.book import BookRepo
    from app.db.session import get_session

    async with get_session() as session:
        book = await BookRepo(session).get(book_id)
    return book is not None and book.status is BookStatus.READY and bool(book.cover_key)


async def amain(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Seed the 100+ book public-domain library")
    parser.add_argument("--limit", type=int, default=0, help="cap number of books (0 = all)")
    parser.add_argument("--no-hd", action="store_true", help="generated covers only (fast)")
    parser.add_argument("--force", action="store_true", help="rebuild even if already seeded")
    args = parser.parse_args(argv)

    from app.core.config import get_settings
    from app.core.logging import configure_logging

    configure_logging(get_settings().log_level)

    entries = load_catalog(CATALOG)
    if args.limit:
        entries = entries[: args.limit]
    use_hd = not args.no_hd
    print(f"seeding {len(entries)} books (hd_covers={use_hd}, force={args.force})…", flush=True)

    _prefetch(entries)

    report: list[dict[str, Any]] = []
    sources: Counter[str] = Counter()
    seeded = skipped = failed = 0

    for i, entry in enumerate(entries, 1):
        prefix = f"[{i}/{len(entries)}] {entry.title[:42]:42}"
        try:
            if not args.force and await _is_seeded(entry.id):
                skipped += 1
                sources["skipped"] += 1
                report.append({"id": entry.id, "title": entry.title, "status": "skipped"})
                print(f"{prefix} · skip (already seeded)", flush=True)
                continue
            epub = download(entry.gutenberg_id, url=entry.epub_url)
            if epub is None:
                failed += 1
                report.append({"id": entry.id, "title": entry.title, "status": "download_failed"})
                print(f"{prefix} · DOWNLOAD FAILED", flush=True)
                continue
            cover_png, cover_source = _cover_for(entry, use_hd=use_hd)
            res = await build_book(
                book_id=entry.id, tag=f"pd{entry.gutenberg_id}", title=entry.title,
                author=entry.author or "Unknown", art=art_direction_for(entry.genre),
                epub=epub, num_pages=PAGES_TO_RASTERISE, cover_png=cover_png,
                cover_source=cover_source,
            )
            if res is None:
                failed += 1
                report.append({"id": entry.id, "title": entry.title, "status": "too_short"})
                print(f"{prefix} · skipped (too short)", flush=True)
                continue
            seeded += 1
            sources[cover_source] += 1
            report.append({
                "id": entry.id, "title": entry.title, "genre": entry.genre,
                "status": "seeded", "cover_source": cover_source,
                "pages": res["pages"], "words": res["words"], "shots": res["shots"],
            })
            print(f"{prefix} · OK cover={cover_source} words={res['words']}", flush=True)
        except Exception as e:  # noqa: BLE001 - one bad book never aborts the seed
            failed += 1
            report.append({"id": entry.id, "title": entry.title, "status": f"error: {e}"})
            print(f"{prefix} · ERROR {e}", flush=True)

    REPORT.write_text(
        json.dumps(
            {"seeded": seeded, "skipped": skipped, "failed": failed,
             "cover_sources": dict(sources), "books": report},
            indent=2, ensure_ascii=False,
        ) + "\n",
        encoding="utf-8",
    )
    total_ready = seeded + skipped
    print(
        f"\n=== LIBRARY SEED: {total_ready} ready ({seeded} new, {skipped} skipped, "
        f"{failed} failed) · covers={dict(sources)} ===",
        flush=True,
    )
    print(f"report → {REPORT}", flush=True)
    return 0 if total_ready >= 100 or args.limit else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(amain()))
