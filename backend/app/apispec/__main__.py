"""``python -m app.apispec`` → the API-spec CLI (snapshot / diff / generate)."""

from __future__ import annotations

import sys

from app.apispec.cli import main

if __name__ == "__main__":
    sys.exit(main())
