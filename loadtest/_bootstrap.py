"""Make ``app.reliability`` importable when running ``python -m loadtest``.

The reusable models live in the backend package (``backend/app/reliability``);
the ``loadtest`` CLI is a thin top-level wrapper around them. When invoked from
the repo root, ``backend/`` is not on ``sys.path`` by default, so this module
prepends it (idempotently). Importing :mod:`loadtest._bootstrap` before any
``app.*`` import is all the CLI entrypoints need.

This is a runtime convenience for the CLI only; the unit tests import
``app.reliability`` directly (they run with ``backend/`` as the rootdir), so they
never touch this shim.
"""

from __future__ import annotations

import sys
from pathlib import Path


def ensure_backend_on_path() -> Path:
    """Prepend ``<repo>/backend`` to ``sys.path`` so ``app.*`` resolves."""
    backend = Path(__file__).resolve().parent.parent / "backend"
    backend_str = str(backend)
    if backend.is_dir() and backend_str not in sys.path:
        sys.path.insert(0, backend_str)
    return backend


ensure_backend_on_path()

__all__ = ["ensure_backend_on_path"]
