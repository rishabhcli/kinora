"""Concrete product read models folded by the :mod:`app.datalayer` runner.

Each module pairs a :class:`~app.datalayer.projector.Projection` (the fold) with a
thin query repository (the read facade the API/UI calls). All three fold the
*real* domain events emitted by the event-sourced aggregates in
:mod:`app.eventsourcing.domain` — decoded from the stored envelope by
:mod:`app.datalayer.envelope` — so the views can never drift from the write side.

* :mod:`app.datalayer.readmodels.render_progress` — a per-book render-progress
  view (planned / accepted / degraded counts + video-seconds + % complete) for
  the library card and the reading-room buffer UI.
* :mod:`app.datalayer.readmodels.session_activity` — a per-session activity view
  (mode, intent, comment + preference counts, lifecycle) for the director bar.
* :mod:`app.datalayer.readmodels.shot_lifecycle` — a per-shot §9.7 lifecycle
  board (current state, attempts, QA score) for the live crew-activity panel.
"""

from __future__ import annotations

from app.datalayer.readmodels.render_progress import (
    RenderProgressProjection,
    RenderProgressRepository,
)
from app.datalayer.readmodels.session_activity import (
    SessionActivityProjection,
    SessionActivityRepository,
)
from app.datalayer.readmodels.shot_lifecycle import (
    ShotLifecycleProjection,
    ShotLifecycleRepository,
)


def all_projections() -> list[object]:
    """Instantiate the three product projections (the default read side)."""
    return [
        RenderProgressProjection(),
        SessionActivityProjection(),
        ShotLifecycleProjection(),
    ]


__all__ = [
    "RenderProgressProjection",
    "RenderProgressRepository",
    "SessionActivityProjection",
    "SessionActivityRepository",
    "ShotLifecycleProjection",
    "ShotLifecycleRepository",
    "all_projections",
]
