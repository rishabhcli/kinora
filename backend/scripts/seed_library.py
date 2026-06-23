#!/usr/bin/env python3
"""Seed Kinora's bundled public-domain library through the live API.

Uploads every committed demo PDF under ``assets/books/`` for the demo account
(``demo@kinora.local`` by default). Skips books whose title already exists on
the shelf so the script is safe to re-run.

Examples::

    make stack-up
    make seed-library

    backend/.venv/bin/python backend/scripts/seed_library.py --via api
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ASSETS = _REPO_ROOT / "assets" / "books"


@dataclass(frozen=True)
class BundledBook:
    pdf: Path
    title: str
    author: str
    art_direction: str


BUNDLED_BOOKS: tuple[BundledBook, ...] = (
    BundledBook(
        pdf=_ASSETS / "the_frog_king.pdf",
        title="The Frog-King",
        author="Brothers Grimm (public domain)",
        art_direction="painterly storybook",
    ),
    BundledBook(
        pdf=_ASSETS / "little_red_riding_hood.pdf",
        title="Little Red Riding Hood",
        author="Brothers Grimm (public domain)",
        art_direction="woodland fairy tale, warm autumn palette",
    ),
)


def seed_library_via_api(
    *,
    api_url: str,
    email: str,
    password: str,
    timeout_s: float,
    books: tuple[BundledBook, ...],
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
        titles = {b["title"] for b in existing.json()}

        failures = 0
        for book in books:
            if not book.pdf.exists():
                print(f"missing PDF: {book.pdf}", file=sys.stderr)
                print("build bundled books first: make demo-pdf", file=sys.stderr)
                failures += 1
                continue
            if book.title in titles:
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
            print(f"uploaded {book.title!r} ({book_id}); ingesting…")

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

        if failures:
            return 2
        print("\n=== SEED LIBRARY OK ===")
        return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Seed Kinora's bundled public-domain library.")
    parser.add_argument("--api-url", default="http://localhost:8000")
    parser.add_argument("--email", default="demo@kinora.local")
    parser.add_argument("--password", default="demo-password-123")
    parser.add_argument("--timeout", type=float, default=900.0, help="seconds per book ingest")
    args = parser.parse_args(argv)
    return seed_library_via_api(
        api_url=args.api_url,
        email=args.email,
        password=args.password,
        timeout_s=args.timeout,
        books=BUNDLED_BOOKS,
    )


if __name__ == "__main__":
    raise SystemExit(main())
