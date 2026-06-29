"""Activity heartbeating — keep a long-running activity's lease alive + detect death.

Long activities (a multi-minute Wan render, a whole-book ingest pass) must tell
the engine "I'm still alive" or the engine can't tell a slow-but-healthy activity
from a crashed worker. The :class:`Heartbeater` is handed to the activity via its
:class:`~app.platform.workflows.activity.ActivityContext`; calling
``await actx.heartbeat(details)`` renews the activity task's lease in the store
and stores progress ``details`` (so a *retried* attempt can resume from the last
checkpoint rather than from scratch — the render pipeline's checkpoint discipline,
generalised).

If an activity stops heartbeating for longer than its ``heartbeat_timeout_s``, the
lease lapses and the store re-delivers the task to another worker — that's the
heartbeat-timeout half of the at-least-once contract. The heartbeat also reports
whether a **cancellation** has been requested, so cooperative activities can stop
early and release reserved budget (mirroring the render queue's cancel-token
checks at safe points).
"""

from __future__ import annotations

from typing import Any

from app.jobs.clock import Clock, SystemClock
from app.platform.workflows.store import WorkflowStore


class Heartbeater:
    """Renews an activity task's lease and records progress details."""

    def __init__(
        self,
        *,
        store: WorkflowStore,
        task_id: str,
        lease_token: str,
        clock: Clock | None = None,
        lease_s: float = 30.0,
    ) -> None:
        self._store = store
        self._task_id = task_id
        self._lease_token = lease_token
        self._clock = clock or SystemClock()
        self._lease_s = lease_s
        self._last_details: Any = None

    @property
    def last_details(self) -> Any:
        """The most recent progress details reported via :meth:`heartbeat`."""
        return self._last_details

    async def heartbeat(self, details: Any = None) -> bool:
        """Renew the lease and record ``details``.

        Returns False if the lease is no longer ours (it lapsed and the task was
        re-claimed elsewhere) — a cooperative activity should then stop, since its
        result will be discarded.
        """
        self._last_details = details
        now = self._clock.now()
        return await self._store.heartbeat_activity_task(
            self._task_id, self._lease_token, now, self._lease_s
        )


__all__ = ["Heartbeater"]
