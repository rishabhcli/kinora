"""Change subscription + hot-reload notification for the runtime config plane.

When an operator flips a flag, the systems that *read* it (the scheduler, a
provider router, an SSE bridge that pushes flag state to the desktop renderer)
need to know without polling. :class:`SubscriptionHub` is a tiny synchronous
pub/sub: callers :meth:`subscribe` a callback and the plane :meth:`publish`es a
:class:`ChangeEvent` on every mutation. Subscribers are invoked in registration
order; a subscriber that raises is isolated (logged, swallowed) so one bad
listener can never break a flag write or starve its siblings.

Deliberately synchronous and infra-free: the hot path is a flag write (rare,
operator-driven), not a request. A caller that needs cross-process fan-out
(Redis pub/sub) layers it on by subscribing a callback that publishes onward —
the hub does not assume a transport.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from app.core.logging import get_logger

logger = get_logger("app.flags.plane.subscriptions")


class ChangeKind(StrEnum):
    """The kind of mutation that produced a :class:`ChangeEvent`."""

    SET_STATIC = "set_static"
    CLEAR_STATIC = "clear_static"
    ADD_RULE = "add_rule"
    REMOVE_RULE = "remove_rule"
    SET_ROLLOUT = "set_rollout"
    CLEAR_ROLLOUT = "clear_rollout"
    CLEAR_FLAG = "clear_flag"
    RELOAD = "reload"  # the whole override layer was replaced (import / hot-reload)


@dataclass(frozen=True, slots=True)
class ChangeEvent:
    """A single notification that a flag's runtime configuration changed."""

    kind: ChangeKind
    flag_key: str | None  # None for a whole-layer RELOAD
    version: int  # the override layer version *after* the change
    actor: str | None = None
    summary: str = ""
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "flag_key": self.flag_key,
            "version": self.version,
            "actor": self.actor,
            "summary": self.summary,
            "detail": dict(self.detail),
        }


#: A subscriber callback. Returning anything is ignored; raising is isolated.
Subscriber = Callable[[ChangeEvent], None]


class SubscriptionHub:
    """A synchronous, fault-isolated fan-out of :class:`ChangeEvent`\\ s."""

    def __init__(self) -> None:
        self._subscribers: list[Subscriber] = []

    def subscribe(self, callback: Subscriber) -> Callable[[], None]:
        """Register ``callback``; returns an idempotent unsubscribe function."""
        self._subscribers.append(callback)

        def _unsubscribe() -> None:
            # already removed -> unsubscribe is idempotent
            with contextlib.suppress(ValueError):
                self._subscribers.remove(callback)

        return _unsubscribe

    @property
    def subscriber_count(self) -> int:
        """How many subscribers are currently registered."""
        return len(self._subscribers)

    def publish(self, event: ChangeEvent) -> None:
        """Notify every subscriber of ``event`` (a raising subscriber is isolated)."""
        # Iterate a copy so a subscriber that (un)subscribes during dispatch is safe.
        for subscriber in list(self._subscribers):
            try:
                subscriber(event)
            except Exception:  # noqa: BLE001 - one bad listener must not break a write
                logger.warning(
                    "flags.plane.subscriber_error",
                    kind=event.kind.value,
                    flag_key=event.flag_key,
                )


__all__ = ["ChangeEvent", "ChangeKind", "SubscriptionHub", "Subscriber"]
