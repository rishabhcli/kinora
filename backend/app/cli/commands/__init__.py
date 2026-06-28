"""Thin argparse command shells for the admin CLI.

Each module registers a top-level subcommand group (``books``, ``budget``,
``queue``, ``canon``, ``users``, ``render``, ``maint``, ``doctor``) onto the
root parser. A shell's only job is to parse flags, call exactly one action
function from :mod:`app.cli.actions`, and hand the typed result to the renderer;
it holds no business logic.
"""

from __future__ import annotations

__all__: list[str] = []
