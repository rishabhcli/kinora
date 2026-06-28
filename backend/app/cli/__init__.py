"""Kinora admin / operations CLI (``python -m app.cli`` / ``kinora-admin``).

A two-layer command surface over the wired composition :class:`Container`:

* ``app.cli.actions.*`` — pure, unit-tested async functions returning typed,
  JSON-serializable result objects (the logic).
* ``app.cli.commands.*`` — thin argparse subcommands that parse flags, call one
  action, and render the result as a ``table`` or ``json`` (the shell).

See ``app/cli/DESIGN.md`` for the architecture and milestone roadmap, and
``kinora.md`` §8/§11/§12 for the domain the operations cover.
"""

from __future__ import annotations

from app.cli.errors import CliError
from app.cli.output import Format, Payload, Renderable, Table, render

__all__ = ["CliError", "Format", "Payload", "Renderable", "Table", "render"]
