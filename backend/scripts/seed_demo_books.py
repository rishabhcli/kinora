#!/usr/bin/env python3
"""Seed both bundled public-domain demo books through the real ingest pipeline."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "backend" / "scripts" / "seed_demo.py"
_BOOKS = (
    (
        _REPO_ROOT / "assets" / "books" / "the_frog_king.pdf",
        "The Frog-King",
        "painterly storybook",
    ),
    (
        _REPO_ROOT / "assets" / "books" / "little_red_riding_hood.pdf",
        "Little Red Riding Hood",
        "woodland fairy tale",
    ),
)


def main() -> int:
    for pdf, title, art in _BOOKS:
        if not pdf.exists():
            print(f"missing PDF: {pdf}", file=sys.stderr)
            print("run: make demo-pdf", file=sys.stderr)
            return 1
        print(f"\n=== seeding {title!r} ===")
        cmd = [
            sys.executable,
            str(_SCRIPT),
            "--pdf",
            str(pdf),
            "--title",
            title,
            "--art-direction",
            art,
            *sys.argv[1:],
        ]
        rc = subprocess.call(cmd, cwd=_REPO_ROOT / "backend")
        if rc != 0:
            return rc
    print("\n=== both demo books seeded ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
