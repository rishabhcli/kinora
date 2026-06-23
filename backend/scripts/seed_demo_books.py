#!/usr/bin/env python3
"""Seed both bundled public-domain demo books through the real ingest flow.

Runs :mod:`seed_demo` once per title against the live API (default) so the demo
library ships with two ready-to-watch fairy tales.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = Path(__file__).resolve().parent / "seed_demo.py"

DEMO_BOOKS = (
    {
        "pdf": _REPO_ROOT / "assets" / "books" / "the_frog_king.pdf",
        "title": "The Frog-King",
        "author_flag": "Brothers Grimm (public domain)",
    },
    {
        "pdf": _REPO_ROOT / "assets" / "books" / "little_red_riding_hood.pdf",
        "title": "Little Red Riding Hood",
        "author_flag": "Brothers Grimm (public domain)",
    },
)


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Seed all bundled Kinora demo books.")
    parser.add_argument("--via", choices=("api", "direct"), default="api")
    parser.add_argument("--api-url", default="http://localhost:8000")
    parser.add_argument("--email", default="demo@kinora.local")
    parser.add_argument("--password", default="demo-password-123")
    parser.add_argument("--timeout", type=float, default=900.0)
    args = parser.parse_args(argv)

    py = _REPO_ROOT / "backend" / ".venv" / "bin" / "python"
    if not py.exists():
        py = Path(sys.executable)

    for book in DEMO_BOOKS:
        if not book["pdf"].exists():
            print(f"demo PDF not found: {book['pdf']}", file=sys.stderr)
            print("build demo PDFs first: make demo-pdf", file=sys.stderr)
            return 1

    for book in DEMO_BOOKS:
        print(f"\n=== Seeding {book['title']!r} ===")
        cmd = [
            str(py),
            str(_SCRIPT),
            f"--via={args.via}",
            f"--api-url={args.api_url}",
            f"--pdf={book['pdf']}",
            f"--title={book['title']}",
            f"--email={args.email}",
            f"--password={args.password}",
            f"--timeout={args.timeout}",
        ]
        result = subprocess.run(cmd, cwd=_REPO_ROOT / "backend", check=False)
        if result.returncode != 0:
            return result.returncode

    print("\n=== ALL DEMO BOOKS SEEDED ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
