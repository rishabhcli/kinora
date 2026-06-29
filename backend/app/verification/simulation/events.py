"""A capturing event publisher — the §5.6 / §12.5 event tap for the simulation.

The reading→scheduler→queue→render→events loop *ends* in events: ``buffer_state``,
``clip_ready``, ``agent_activity``, ``budget_low`` (kinora.md §5.6, §9.8). The
real :class:`~app.scheduler.events.SessionEventPublisher` pushes these to redis
pub/sub for the client. In the simulation we substitute this capturing publisher
so the whole event stream is recorded on the virtual timeline and the invariants
can assert against it (e.g. "every promoted shot eventually emits ``clip_ready``").

It implements the :class:`~app.scheduler.events.SessionEventPublisher` protocol
(``async def publish(session_id, message) -> int``) so the real scheduler accepts
it unchanged.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class CapturedEvent:
    """One published event, timestamped on the virtual clock."""

    t_ms: int
    session_id: str
    type: str
    payload: dict[str, Any]


class CapturingEventPublisher:
    """Records every published session event on the virtual timeline.

    The ``now_ms`` callable timestamps each event so the trace is ordered on the
    same clock as everything else (network deliveries, worker completions). The
    scheduler calls :meth:`publish`; tests and invariants read :attr:`events`.
    """

    __slots__ = ("_now_ms", "events")

    def __init__(self, now_ms: Callable[[], int]) -> None:
        self._now_ms = now_ms
        self.events: list[CapturedEvent] = []

    async def publish(self, session_id: str, message: dict[str, Any]) -> int:
        # The scheduler tags events with ``"event"``; the render/clip path uses
        # ``"type"``. Capture whichever is present so the trace is well-named.
        kind = message.get("type") or message.get("event") or "unknown"
        self.events.append(
            CapturedEvent(
                t_ms=self._now_ms(),
                session_id=session_id,
                type=str(kind),
                payload=dict(message),
            )
        )
        return 1

    def of_type(self, event_type: str) -> list[CapturedEvent]:
        """All captured events of a given ``type`` (e.g. ``"buffer_state"``)."""
        return [e for e in self.events if e.type == event_type]

    def type_counts(self) -> dict[str, int]:
        """A ``{type: n}`` histogram of captured events."""
        counts: dict[str, int] = {}
        for e in self.events:
            counts[e.type] = counts.get(e.type, 0) + 1
        return counts


__all__ = ["CapturedEvent", "CapturingEventPublisher"]
