#!/usr/bin/env python3
"""Seed both bundled public-domain demo books through the real ingest flow."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SEED = _REPO_ROOT / "backend" / "scripts" / "seed_demo.py"
_BOOKS = (
    (
        _REPO_ROOT / "assets" / "books" / "the_frog_king.pdf",
        "The Frog-King",
        "Brothers Grimm (public domain)",
    ),
    (
        _REPO_ROOT / "assets" / "books" / "little_red_riding_hood.pdf",
        "Little Red Riding Hood",
        "Brothers Grimm (public domain)",
    ),
)


def main(argv: list[str] | None = None) -> int:
    extra = list(argv or sys.argv[1:])
    for pdf, title, _author in _BOOKS:
        if not pdf.exists():
            print(f"demo PDF not found: {pdf}", file=sys.stderr)
            print("build it first: make demo-pdf", file=sys.stderr)
            return 1
        cmd = [
            sys.executable,
            str(_SEED),
            "--pdf",
            str(pdf),
            "--title",
            title,
            *extra,
        ]
        print(f"\n=== seeding {title!r} ===")
        rc = subprocess.call(cmd)
        if rc != 0:
            return rc
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
