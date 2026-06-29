"""``ActivityContext`` — what an activity function receives.

Activities are the *non-deterministic* half of the engine: they run outside the
replay sandbox and do the real I/O (DashScope calls, DB writes, ffmpeg, MCP canon
queries). Because they're at-least-once, an activity should be **idempotent** — re-
running it must be safe. The context gives an activity everything it needs to be a
good durable citizen:

* identity (``workflow_id`` / ``run_id`` / ``activity_type`` / ``attempt``) so logs
  and side effects can be keyed and made idempotent;
* :meth:`heartbeat` to keep its lease alive and checkpoint progress, plus
  :meth:`is_cancellation_requested`-style cooperation via the heartbeat return.

An activity signature is ``async def fn(actx: ActivityContext, *args, **kwargs)``.
The first positional parameter is always the context; the rest are the JSON-able
arguments the workflow passed to ``ctx.execute_activity(type, *args, **kwargs)``.
"""

from __future__ import annotations

from typing import Any

from app.platform.workflows.heartbeat import Heartbeater


class ActivityContext:
    """The handle an activity uses to heartbeat, checkpoint, and identify itself."""

    __slots__ = ("workflow_id", "run_id", "activity_type", "attempt", "_heartbeater")

    def __init__(
        self,
        *,
        workflow_id: str,
        run_id: str,
        activity_type: str,
        attempt: int,
        heartbeater: Heartbeater,
    ) -> None:
        self.workflow_id = workflow_id
        self.run_id = run_id
        self.activity_type = activity_type
        self.attempt = attempt
        self._heartbeater = heartbeater

    async def heartbeat(self, details: Any = None) -> bool:
        """Renew the lease and record progress ``details`` (see :class:`Heartbeater`)."""
        return await self._heartbeater.heartbeat(details)

    @property
    def last_heartbeat_details(self) -> Any:
        """Progress details from the *previous* attempt, if the engine restored them."""
        return self._heartbeater.last_details


__all__ = ["ActivityContext"]
