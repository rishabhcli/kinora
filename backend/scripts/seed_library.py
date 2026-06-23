#!/usr/bin/env python3
"""Seed Kinora's bundled public-domain library (both demo books).

Loads **The Frog-King** and **Little Red Riding Hood** through the real HTTP
upload flow (``POST /api/books``), the same path a desktop user takes. Skips a
title that is already on the shelf for the demo user so re-runs stay idempotent.

Examples::

    make demo-pdfs
    make stack-up
    backend/.venv/bin/python backend/scripts/seed_library.py
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True, slots=True)
class DemoBook:
    pdf: Path
    title: str
    author: str
    art_direction: str


DEMO_BOOKS: tuple[DemoBook, ...] = (
    DemoBook(
        pdf=_REPO_ROOT / "assets" / "books" / "the_frog_king.pdf",
        title="The Frog-King",
        author="Brothers Grimm (public domain)",
        art_direction="painterly storybook",
    ),
    DemoBook(
        pdf=_REPO_ROOT / "assets" / "books" / "little_red_riding_hood.pdf",
        title="Little Red Riding Hood",
        author="Brothers Grimm (public domain)",
        art_direction="warm woodland fairy tale",
    ),
)


def seed_library(
    *,
    api_url: str,
    email: str,
    password: str,
    books: tuple[DemoBook, ...],
    timeout_s: float,
) -> int:
    import httpx

    base = api_url.rstrip("/")
    with httpx.Client(base_url=base, timeout=30.0) as http:
        reg = http.post("/api/auth/register", json={"email": email, "password": password})
        if reg.status_code not in (200, 201, 409):
            print(f"register failed: {reg.status_code} {reg.text}", file=sys.stderr)
            return 1
        login = http.post("/api/auth/login", json={"email": email, "password": password})
        if login.status_code != 200:
            print(f"login failed: {login.status_code} {login.text}", file=sys.stderr)
            return 1
        headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

        existing = http.get("/api/books", headers=headers)
        existing.raise_for_status()
        on_shelf = {b["title"] for b in existing.json()}

        failures = 0
        for book in books:
            if not book.pdf.exists():
                print(f"PDF not found: {book.pdf}", file=sys.stderr)
                print("build it first: make demo-pdfs", file=sys.stderr)
                failures += 1
                continue
            if book.title in on_shelf:
                print(f"skip (already on shelf): {book.title!r}")
                continue

            files = {"file": (book.pdf.name, book.pdf.read_bytes(), "application/pdf")}
            data = {
                "title": book.title,
                "author": book.author,
                "art_direction": book.art_direction,
            }
            up = http.post("/api/books", files=files, data=data, headers=headers)
            if up.status_code not in (200, 201):
                print(f"upload failed for {book.title}: {up.status_code} {up.text}", file=sys.stderr)
                failures += 1
                continue

            payload = up.json()
            book_id = payload["id"]
            print(f"uploaded {book.title!r} as {book_id!r}; ingesting...")

            deadline = time.monotonic() + timeout_s
            status = payload["status"]
            while time.monotonic() < deadline:
                resp = http.get(f"/api/books/{book_id}", headers=headers)
                resp.raise_for_status()
                payload = resp.json()
                status = payload["status"]
                stage = payload.get("stage")
                pct = payload.get("progress")
                print(f"  {book.title}: status={status} stage={stage} pct={pct}")
                if status in ("ready", "failed"):
                    break
                time.sleep(3.0)

            if status != "ready":
                print(f"ingest did not reach ready for {book.title} (status={status})", file=sys.stderr)
                failures += 1
                continue

            shots = http.get(f"/api/books/{book_id}/shots", headers=headers).json()
            print(f"  ready: pages={payload.get('num_pages')} shots={len(shots.get('shots', []))}")

        if failures:
            return 2
        print("\n=== LIBRARY SEED OK ===")
        return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Seed Kinora's bundled demo library.")
    parser.add_argument("--api-url", default="http://localhost:8000")
    parser.add_argument("--email", default="demo@kinora.local")
    parser.add_argument("--password", default="demo-password-123")
    parser.add_argument("--timeout", type=float, default=900.0)
    args = parser.parse_args(argv)
    return seed_library(
        api_url=args.api_url,
        email=args.email,
        password=args.password,
        books=DEMO_BOOKS,
        timeout_s=args.timeout,
    )


if __name__ == "__main__":
    raise SystemExit(main())
