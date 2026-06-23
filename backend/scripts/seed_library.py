#!/usr/bin/env python3
"""Seed Kinora's bundled public-domain library through the real ingest flow.

Loads every committed demo PDF under ``assets/books/`` (Frog-King, Little Red
Riding Hood, …) via the same HTTP upload path a desktop user would take.

Examples::

    backend/.venv/bin/python backend/scripts/seed_library.py
    backend/.venv/bin/python backend/scripts/seed_library.py --via direct
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ASSETS = _REPO_ROOT / "assets" / "books"


@dataclass(frozen=True)
class BundledBook:
    pdf: str
    title: str
    author: str
    art_direction: str = "painterly storybook"


BUNDLED_BOOKS: tuple[BundledBook, ...] = (
    BundledBook(
        pdf="the_frog_king.pdf",
        title="The Frog-King",
        author="Brothers Grimm (public domain)",
    ),
    BundledBook(
        pdf="little_red_riding_hood.pdf",
        title="Little Red Riding Hood",
        author="Brothers Grimm (public domain)",
    ),
)


def _resolve_pdf(name: str) -> Path:
    path = _ASSETS / name
    if not path.exists():
        print(f"demo PDF not found: {path}", file=sys.stderr)
        print("build it first: make demo-pdf", file=sys.stderr)
        raise SystemExit(1)
    return path


def seed_via_api(
    *,
    api_url: str,
    email: str,
    password: str,
    timeout_s: float,
) -> int:
    import httpx

    from scripts.seed_demo import seed_via_api as seed_one

    for book in BUNDLED_BOOKS:
        pdf_path = _resolve_pdf(book.pdf)
        print(f"\n=== Uploading {book.title!r} ===")
        code = seed_one(
            api_url=api_url,
            pdf_path=pdf_path,
            email=email,
            password=password,
            title=book.title,
            art_direction=book.art_direction,
            timeout_s=timeout_s,
        )
        if code != 0:
            return code
    print("\n=== LIBRARY SEED OK ===")
    return 0


async def _seed_direct() -> int:
    from scripts.seed_demo import _seed_direct

    for book in BUNDLED_BOOKS:
        pdf_path = _resolve_pdf(book.pdf)
        print(f"\n=== Ingesting {book.title!r} (direct) ===")
        code = await _seed_direct(
            pdf_path=pdf_path,
            title=book.title,
            art_direction=book.art_direction,
        )
        if code != 0:
            return code
    print("\n=== LIBRARY SEED OK (direct) ===")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python backend/scripts/seed_library.py",
        description="Load every bundled public-domain book through the real ingest flow.",
    )
    parser.add_argument(
        "--via", choices=("api", "direct"), default="api", help="load path (default: api)"
    )
    parser.add_argument("--api-url", default="http://localhost:8000", help="gateway base URL")
    parser.add_argument("--email", default="demo@kinora.local")
    parser.add_argument("--password", default="demo-password-123")
    parser.add_argument(
        "--timeout", type=float, default=900.0, help="seconds to wait per book (api mode)"
    )
    args = parser.parse_args(argv)

    if args.via == "api":
        return seed_via_api(
            api_url=args.api_url,
            email=args.email,
            password=args.password,
            timeout_s=args.timeout,
        )
    return asyncio.run(_seed_direct())


if __name__ == "__main__":
    raise SystemExit(main())
