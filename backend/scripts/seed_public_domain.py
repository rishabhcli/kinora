#!/usr/bin/env python3
"""Seed several full public-domain works (Project Gutenberg EPUBs) through the
real ingest flow, so the scroll-to-generate reading room has actual books to
drive (each gets a shot list + source_span_index built by ingest).

Run against a running stack (api on :8000), live video on:
    backend/.venv/bin/python backend/scripts/seed_public_domain.py

Ingest is slow and the image model is 429-prone, so books are uploaded ONE AT A
TIME (shortest first) and each is polled to ready/failed with a long timeout.
"""
from __future__ import annotations

import os
import sys
import time
import urllib.request
from pathlib import Path

import httpx

API = os.environ.get("KINORA_API_URL", "http://localhost:8000")
EMAIL = os.environ.get("KINORA_DEMO_EMAIL", "demo@kinora.local")
PASSWORD = os.environ.get("KINORA_DEMO_PASSWORD", "demo-password-123")

# (gutenberg_id, title, author, art_direction) — shortest complete works first
# so at least the early ones finish quickly even if the image quota throttles.
BOOKS = [
    (1952, "The Yellow Wallpaper", "Charlotte Perkins Gilman", "painterly gothic, muted candlelight"),
    (5200, "The Metamorphosis", "Franz Kafka", "surreal muted, uneasy shadows"),
    (43, "The Strange Case of Dr Jekyll and Mr Hyde", "Robert Louis Stevenson", "victorian noir, gaslit fog"),
    (46, "A Christmas Carol", "Charles Dickens", "warm victorian, snow and hearthlight"),
    (11, "Alice's Adventures in Wonderland", "Lewis Carroll", "whimsical storybook, dreamlike"),
]
DEST = Path(__file__).resolve().parents[2] / "assets" / "books" / "public-domain"


def download(gid: int) -> Path | None:
    DEST.mkdir(parents=True, exist_ok=True)
    out = DEST / f"pg{gid}.epub"
    # Buffered httpx GET with redirect-follow is far more reliable than urllib
    # (which truncated mid-stream); always re-fetch so a prior partial can't stick.
    for url in (
        f"https://www.gutenberg.org/cache/epub/{gid}/pg{gid}.epub",
        f"https://www.gutenberg.org/ebooks/{gid}.epub.images",
        f"https://www.gutenberg.org/ebooks/{gid}.epub3.images",
    ):
        print(f"download {url}", flush=True)
        try:
            with httpx.Client(follow_redirects=True, timeout=180.0, headers={"User-Agent": "Mozilla/5.0 (Kinora seed)"}) as dc:
                r = dc.get(url)
                r.raise_for_status()
            if len(r.content) < 20000:
                print(f"  too small ({len(r.content)}B), trying next", flush=True)
                continue
            out.write_bytes(r.content)
            print(f"  ok ({len(r.content) // 1024} KiB)", flush=True)
            return out
        except Exception as e:  # noqa: BLE001
            print(f"  failed: {e}", flush=True)
    return None


def main() -> int:
    with httpx.Client(base_url=API, timeout=60.0) as c:
        r = c.post("/api/auth/login", json={"email": EMAIL, "password": PASSWORD})
        if r.status_code != 200:
            c.post("/api/auth/register", json={"email": EMAIL, "password": PASSWORD})
            r = c.post("/api/auth/login", json={"email": EMAIL, "password": PASSWORD})
        r.raise_for_status()
        c.headers["Authorization"] = f"Bearer {r.json()['access_token']}"
        print(f"logged in as {EMAIL}", flush=True)

        results: list[tuple[str, str]] = []
        for gid, title, author, art in BOOKS:
            path = download(gid)
            if not path:
                results.append((title, "download_failed"))
                continue
            print(f"\n=== upload {title} ({path.stat().st_size // 1024} KiB) ===", flush=True)
            with open(path, "rb") as f:
                up = c.post(
                    "/api/books",
                    files={"file": (path.name, f, "application/epub+zip")},
                    data={"title": title, "author": author, "art_direction": art},
                    timeout=180.0,
                )
            if up.status_code not in (200, 201):
                print(f"  upload failed {up.status_code}: {up.text[:200]}", flush=True)
                results.append((title, f"upload_{up.status_code}"))
                continue
            bid = up.json()["id"]
            print(f"  book {bid} ingesting…", flush=True)
            deadline = time.time() + 2400
            status = "timeout"
            while time.time() < deadline:
                time.sleep(8)
                try:
                    b = c.get(f"/api/books/{bid}").json()
                except Exception as e:  # noqa: BLE001
                    print(f"  poll error: {e}", flush=True)
                    continue
                status = b.get("status", "?")
                print(f"  {title}: status={status} progress={b.get('progress')} stage={b.get('stage')}", flush=True)
                if status in ("ready", "failed"):
                    break
            results.append((title, status))

        print("\n=== SEED SUMMARY ===", flush=True)
        for title, st in results:
            print(f"  {st:16s} {title}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
