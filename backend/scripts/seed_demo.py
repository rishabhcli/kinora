#!/usr/bin/env python3
"""Seed Kinora with the bundled public-domain demo book and verify Phase A.

Two modes, both exercising the **real** ingest pipeline (PyMuPDF extract ->
Qwen-VL page analysis -> canon build -> Adapter shot list + source-span index ->
identity lock of keyframes + voices, kinora.md §9.1) with **no video spend**
(``KINORA_LIVE_VIDEO`` stays off; ingest is token-only):

* ``--via api`` (default): drive the live gateway over HTTP exactly like a
  browser would — register a user, log in, ``POST /api/books`` with the demo
  PDF, then poll ``GET /api/books/{id}`` until the book is ``ready`` and print
  the projected shelf / shot / canon summary. Use this against the Docker
  Compose stack (``make stack-up`` then ``make migrate``).

* ``--via direct``: skip the HTTP layer and call the ingest **service**
  in-process against the configured Postgres + object store + DashScope — handy
  for local verification from the venv without a running API. Prints the
  :class:`IngestResult` counts (entities, scenes, beats, shots, spans, locked
  principals).

Examples::

    # against a running stack (api on :8000)
    backend/.venv/bin/python backend/scripts/seed_demo.py --via api

    # in-process against the configured infra (no server needed)
    backend/.venv/bin/python backend/scripts/seed_demo.py --via direct

Configuration is the usual :class:`app.core.config.Settings` (env / backend/.env):
``DATABASE_URL``, ``REDIS_URL``, ``S3_*``, and ``DASHSCOPE_API_KEY``.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

# Default to the committed demo books (repo-root/assets/books/*.pdf).
_REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PDF = _REPO_ROOT / "assets" / "books" / "the_frog_king.pdf"
SECOND_PDF = _REPO_ROOT / "assets" / "books" / "little_red_riding_hood.pdf"
DEFAULT_TITLE = "The Frog-King"
SECOND_TITLE = "Little Red Riding Hood"
DEFAULT_ART = "painterly storybook"
SECOND_ART = "enchanted forest storybook"


# --------------------------------------------------------------------------- #
# Mode 1: drive the live HTTP gateway (the real product flow)
# --------------------------------------------------------------------------- #


def seed_via_api(
    *,
    api_url: str,
    pdf_path: Path,
    email: str,
    password: str,
    title: str,
    art_direction: str,
    author: str,
    timeout_s: float,
) -> int:
    """Register -> upload -> poll the live API until the book is ready."""
    import httpx

    pdf_bytes = pdf_path.read_bytes()
    base = api_url.rstrip("/")
    with httpx.Client(base_url=base, timeout=30.0) as http:
        # Register is idempotent for our purposes; ignore a 409 (already exists).
        reg = http.post("/api/auth/register", json={"email": email, "password": password})
        if reg.status_code not in (200, 201, 409):
            print(f"register failed: {reg.status_code} {reg.text}", file=sys.stderr)
            return 1
        login = http.post("/api/auth/login", json={"email": email, "password": password})
        if login.status_code != 200:
            print(f"login failed: {login.status_code} {login.text}", file=sys.stderr)
            return 1
        headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

        files = {"file": (pdf_path.name, pdf_bytes, "application/pdf")}
        data = {
            "title": title,
            "author": author,
            "art_direction": art_direction,
        }
        up = http.post("/api/books", files=files, data=data, headers=headers)
        if up.status_code not in (200, 201):
            print(f"upload failed: {up.status_code} {up.text}", file=sys.stderr)
            return 1
        book = up.json()
        book_id = book["id"]
        print(f"uploaded book {book_id!r} (status={book['status']}); ingesting...")

        deadline = time.monotonic() + timeout_s
        status = book["status"]
        while time.monotonic() < deadline:
            resp = http.get(f"/api/books/{book_id}", headers=headers)
            resp.raise_for_status()
            payload = resp.json()
            status = payload["status"]
            stage = payload.get("stage")
            pct = payload.get("progress")
            print(f"  status={status} stage={stage} pct={pct}")
            if status in ("ready", "failed"):
                break
            time.sleep(3.0)

        if status != "ready":
            print(f"ingest did not reach ready (status={status})", file=sys.stderr)
            return 2

        shots = http.get(f"/api/books/{book_id}/shots", headers=headers).json()
        canon = http.get(f"/api/books/{book_id}/canon", headers=headers).json()
        print("\n=== SEED OK (via api) ===")
        print(f"  book_id:   {book_id}")
        print(f"  title:     {payload['title']}")
        print(f"  pages:     {payload.get('num_pages')}")
        print(f"  shots:     {len(shots.get('shots', []))}")
        print(f"  canon docs:{len(canon.get('keys', []))}")
        return 0


# --------------------------------------------------------------------------- #
# Mode 2: run the ingest service directly (no HTTP server required)
# --------------------------------------------------------------------------- #


async def _seed_direct(
    *, pdf_path: Path, title: str, art_direction: str, author: str
) -> int:
    from app.core.config import get_settings
    from app.core.logging import configure_logging
    from app.db.base import new_id
    from app.db.models.enums import BookStatus
    from app.db.repositories.book import BookRepo
    from app.db.session import get_session
    from app.ingest.service import ingest_pdf
    from app.providers import create_providers
    from app.storage.object_store import ObjectStore, keys

    settings = get_settings()
    configure_logging(settings.log_level)
    pdf_bytes = pdf_path.read_bytes()

    providers = create_providers(settings)
    store = ObjectStore.from_settings(settings)
    # Idempotent bucket create so a fresh MinIO/OSS works out of the box.
    try:
        await asyncio.to_thread(store.ensure_bucket)
    except Exception as exc:  # noqa: BLE001 - bucket may be pre-provisioned/read-only
        print(f"warning: ensure_bucket failed ({exc}); continuing", file=sys.stderr)

    book_id = new_id()
    pdf_key = keys.pdf(book_id)
    await asyncio.to_thread(store.put_bytes, pdf_key, pdf_bytes, "application/pdf")
    async with get_session() as session:
        await BookRepo(session).create(
            title=title,
            author=author,
            source_pdf_key=pdf_key,
            status=BookStatus.IMPORTING,
            art_direction=art_direction,
            book_id=book_id,
        )

    async def progress(stage: str, pct: float) -> None:
        print(f"  ingest stage={stage} pct={pct:.2f}")

    try:
        result = await ingest_pdf(
            book_id,
            pdf_bytes,
            providers=providers,
            blob_store=store,
            settings=settings,
            session_factory=get_session,
            progress=progress,
        )
    finally:
        await providers.aclose()

    print("\n=== SEED OK (via direct ingest service) ===")
    for key, value in result.model_dump().items():
        print(f"  {key}: {value}")
    return 0 if result.status == "ready" else 2


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python backend/scripts/seed_demo.py",
        description="Load Kinora's public-domain demo book through the real ingest flow.",
    )
    parser.add_argument(
        "--via", choices=("api", "direct"), default="api", help="load path (default: api)"
    )
    parser.add_argument("--api-url", default="http://localhost:8000", help="gateway base URL")
    parser.add_argument("--pdf", type=Path, default=DEFAULT_PDF, help="demo PDF path")
    parser.add_argument("--title", default=DEFAULT_TITLE)
    parser.add_argument("--art-direction", default=DEFAULT_ART)
    parser.add_argument(
        "--author",
        default="Brothers Grimm (public domain)",
        help="book author metadata",
    )
    parser.add_argument(
        "--all-books",
        action="store_true",
        help="seed both bundled public-domain demo books (Frog-King + Little Red Riding Hood)",
    )
    parser.add_argument("--email", default="demo@kinora.local")
    parser.add_argument("--password", default="demo-password-123")
    parser.add_argument(
        "--timeout", type=float, default=900.0, help="seconds to wait for ingest (api mode)"
    )
    args = parser.parse_args(argv)

    pdf_path = args.pdf if args.pdf.is_absolute() else (Path.cwd() / args.pdf)
    if not pdf_path.exists():
        print(f"demo PDF not found: {pdf_path}", file=sys.stderr)
        print("build it first: make demo-pdf", file=sys.stderr)
        return 1

    books: list[tuple[Path, str, str, str]] = [
        (pdf_path, args.title, args.art_direction, args.author),
    ]
    if args.all_books:
        if not SECOND_PDF.exists():
            print(f"second demo PDF not found: {SECOND_PDF}", file=sys.stderr)
            print("build it first: make demo-pdf", file=sys.stderr)
            return 1
        books.append((SECOND_PDF, SECOND_TITLE, SECOND_ART, args.author))

    exit_code = 0
    for book_pdf, title, art, author in books:
        if args.via == "api":
            code = seed_via_api(
                api_url=args.api_url,
                pdf_path=book_pdf,
                email=args.email,
                password=args.password,
                title=title,
                art_direction=art,
                author=author,
                timeout_s=args.timeout,
            )
        else:
            code = asyncio.run(
                _seed_direct(pdf_path=book_pdf, title=title, art_direction=art, author=author)
            )
        if code != 0:
            exit_code = code
            break
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
