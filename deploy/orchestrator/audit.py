"""Append-only deploy audit trail (kinora.md §12.5 observability).

Every state transition and decision the orchestrator makes is recorded as an
immutable :class:`~deploy.orchestrator.models.DeployEvent`. The trail is the
forensic record behind a rollout: *why* did this deploy roll back, which SLO
breached, at what sample, with what canary weight.

The trail writes through a :class:`AuditSink` Protocol. The default
:class:`InMemoryAuditSink` is used by tests and the simulator; production wires
a sink that also ships events to structured logging / a metrics backend. The
sink is intentionally tiny so a real one (e.g. writing JSON lines to OSS, or
emitting structlog records) is a few lines.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from typing import Protocol, runtime_checkable

from deploy.orchestrator.models import DeployEvent, DeployState


@runtime_checkable
class AuditSink(Protocol):
    """A destination for audit events. Must be append-only and total-ordered."""

    def append(self, event: DeployEvent) -> None:
        """Persist one event. Implementations must not mutate ``event``."""
        ...

    def events(self) -> Iterable[DeployEvent]:
        """Return all events in append order."""
        ...


class InMemoryAuditSink:
    """A list-backed audit sink (default for tests + the simulator)."""

    __slots__ = ("_events",)

    def __init__(self) -> None:
        self._events: list[DeployEvent] = []

    def append(self, event: DeployEvent) -> None:
        self._events.append(event)

    def events(self) -> list[DeployEvent]:
        return list(self._events)

    def __len__(self) -> int:
        return len(self._events)

    def __iter__(self) -> Iterator[DeployEvent]:
        return iter(self._events)


class AuditTrail:
    """Sequenced, monotonic-clocked writer over an :class:`AuditSink`.

    Stamps each event with a monotonically increasing ``seq`` and the caller's
    ``now()`` time. Time is injected (no implicit ``time.time()``) so the
    simulator and tests run on a virtual clock and the ordering is deterministic.
    """

    __slots__ = ("_sink", "_now", "_seq", "_deploy_id")

    def __init__(
        self,
        deploy_id: str,
        *,
        sink: AuditSink | None = None,
        now: Callable[[], float],
    ) -> None:
        self._sink: AuditSink = sink if sink is not None else InMemoryAuditSink()
        self._now = now
        self._seq = 0
        self._deploy_id = deploy_id

    @property
    def sink(self) -> AuditSink:
        return self._sink

    def record(
        self,
        state: DeployState,
        kind: str,
        message: str,
        **detail: object,
    ) -> DeployEvent:
        """Append one event, returning the stamped record."""
        self._seq += 1
        event = DeployEvent(
            seq=self._seq,
            at=self._now(),
            deploy_id=self._deploy_id,
            state=state,
            kind=kind,
            message=message,
            detail=dict(detail),
        )
        self._sink.append(event)
        return event

    def events(self) -> list[DeployEvent]:
        return list(self._sink.events())

    def by_kind(self, kind: str) -> list[DeployEvent]:
        return [e for e in self._sink.events() if e.kind == kind]

    def last(self) -> DeployEvent | None:
        evs = list(self._sink.events())
        return evs[-1] if evs else None

    def render(self) -> str:
        """A compact human-readable transcript of the deployment."""
        lines = []
        for e in self._sink.events():
            extra = ""
            if e.detail:
                items = ", ".join(f"{k}={v}" for k, v in e.detail.items())
                extra = f" [{items}]"
            lines.append(
                f"#{e.seq:<3} t={e.at:7.2f} {e.state.value:<12} {e.kind}: {e.message}{extra}"
            )
        return "\n".join(lines)
