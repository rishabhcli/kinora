"""``python -m app.cli`` entrypoint."""

from __future__ import annotations

from app.cli.main import main

if __name__ == "__main__":
    raise SystemExit(main())
