"""Pure action functions for the admin CLI.

Each module here is the *logic* layer: async functions taking the collaborators
they need (a :class:`~app.composition.Container`, or a session + repositories)
and returning typed, JSON-serializable result objects that implement
:class:`~app.cli.output.Renderable`. They contain no argv parsing and no
printing, so they are unit-testable in isolation.
"""

from __future__ import annotations

__all__: list[str] = []
