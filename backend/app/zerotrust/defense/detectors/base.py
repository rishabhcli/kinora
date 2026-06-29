"""The detector contract every threat detector binds to.

A detector is a small online state machine: feed it events, get alerts. The
engine treats every detector through the :class:`Detector` protocol, so adding a
new detector is purely additive — register it and it joins the fan-out.

:class:`DetectorBase` provides the shared scaffolding (name, clock, an
expiry-driven sweep of stale per-key state) so concrete detectors only express
their detection logic.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable
from typing import Protocol, runtime_checkable

from ..clock import Clock, SystemClock
from ..types import Alert, SecurityEvent


@runtime_checkable
class Detector(Protocol):
    """A streaming detector consumed by the engine."""

    name: str

    def consumes(self, event: SecurityEvent) -> bool:
        """Whether this detector is interested in ``event`` (cheap prefilter)."""

    def observe(self, event: SecurityEvent) -> Iterable[Alert]:
        """Fold ``event`` into state and yield any alerts it triggers."""

    def sweep(self, now_mono: float) -> None:
        """Drop per-key state idle past its TTL to bound memory."""


class DetectorBase(ABC):
    """Shared base for concrete detectors.

    Subclasses set :attr:`name`, declare which :class:`EventKind`\\ s they handle
    via :attr:`kinds`, and implement :meth:`_observe`. The base wires the clock
    and a default no-op sweep that subclasses override when they keep per-key
    state.
    """

    name: str = "detector"
    #: Event kinds this detector consumes; empty means "all".
    kinds: frozenset = frozenset()

    def __init__(self, *, clock: Clock | None = None) -> None:
        self.clock: Clock = clock or SystemClock()

    def consumes(self, event: SecurityEvent) -> bool:
        return not self.kinds or event.kind in self.kinds

    def observe(self, event: SecurityEvent) -> Iterable[Alert]:
        if not self.consumes(event):
            return ()
        return self._observe(event)

    @abstractmethod
    def _observe(self, event: SecurityEvent) -> Iterable[Alert]:
        """Detection logic; only called for events this detector consumes."""
        raise NotImplementedError

    def sweep(self, now_mono: float) -> None:  # noqa: B027 - intentional no-op default
        """Default: keep no expiring state. Stateful detectors override."""
        return None
