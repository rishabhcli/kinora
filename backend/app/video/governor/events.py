"""Governor events — SLA/quota breaches and capacity alerts (structured, hashable).

The governor never *acts* on a breach itself (it does not page anyone or flip a
provider off); it emits a typed event so the operator layer — dashboards, the
§12.5 metrics panel, an alerting pipeline — can decide. Every event carries the
provider, a machine ``code``, a severity, the offending observed/limit values, and
the time it fired, so it round-trips cleanly to a log line or a Redis pub-sub
message.

A :class:`GovernorEventBus` fans events out to registered sinks. The default sink
is a bounded in-memory ring (inspectable in tests); a deployment also registers a
structlog sink and/or a Redis-publish sink. Sinks are plain callables so they are
trivial to fake and the bus never imports structlog or redis.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from enum import IntEnum, StrEnum


class Severity(IntEnum):
    """Ordered severity so sinks can threshold (``>= WARNING``) numerically."""

    INFO = 10
    WARNING = 20
    CRITICAL = 30


class EventCode(StrEnum):
    """The machine-stable taxonomy of governor events."""

    # -- quota --------------------------------------------------------------
    QUOTA_NEAR_LIMIT = "quota.near_limit"  # crossed an alert fraction of a quota
    QUOTA_EXCEEDED = "quota.exceeded"  # an admission was refused by a quota
    # -- throttle / rate-limit ---------------------------------------------
    THROTTLE_BACKOFF = "throttle.backoff"  # a 429/Retry-After tripped a backoff
    THROTTLE_RECOVERED = "throttle.recovered"  # pacing returned to baseline
    # -- SLA ----------------------------------------------------------------
    SLA_ERROR_BUDGET_LOW = "sla.error_budget_low"  # error budget burning down
    SLA_BREACH = "sla.breach"  # error budget exhausted / grade fell to F
    SLA_RECOVERED = "sla.recovered"  # grade climbed back to healthy
    # -- fair-share ---------------------------------------------------------
    FAIRSHARE_STARVATION = "fairshare.starvation"  # a tenant waited past its cap


@dataclass(frozen=True, slots=True)
class GovernorEvent:
    """One governance signal about a provider (immutable, log/pubsub-ready)."""

    code: EventCode
    severity: Severity
    provider: str
    message: str
    at: float
    #: The observed value that triggered the event (latency ms, count, fraction…).
    observed: float | None = None
    #: The limit/target the observed value was compared against.
    limit: float | None = None
    #: Optional dimension the event is scoped to (e.g. a quota window or tenant).
    scope: str | None = None
    #: Free-form structured extras (counts only — never prompt content).
    detail: dict[str, object] = field(default_factory=dict)

    def as_log_fields(self) -> dict[str, object]:
        """A structured-log-safe flattening of the event."""
        fields = asdict(self)
        fields["severity"] = self.severity.name
        # Drop empty optionals so log lines stay terse.
        return {k: v for k, v in fields.items() if v not in (None, {}, "")}


#: A sink consumes one event. Sinks must not raise — the bus isolates failures.
EventSink = Callable[[GovernorEvent], None]


class GovernorEventBus:
    """Fan events out to sinks, keeping a bounded in-memory tail for inspection.

    The ring buffer (default 256 entries) is the always-on sink so tests and a
    ``/diagnostics`` endpoint can read recent events without wiring a real sink.
    Additional sinks (structlog, Redis publish) are registered with
    :meth:`add_sink`. A sink that raises is swallowed and recorded in
    :attr:`sink_errors` so one bad sink can't break governance.
    """

    def __init__(self, *, history: int = 256) -> None:
        self._ring: deque[GovernorEvent] = deque(maxlen=max(1, history))
        self._sinks: list[EventSink] = []
        self.sink_errors = 0
        self.emitted = 0

    def add_sink(self, sink: EventSink) -> None:
        """Register an additional event sink."""
        self._sinks.append(sink)

    def emit(self, event: GovernorEvent) -> None:
        """Record ``event`` in the ring and fan it to every sink."""
        self._ring.append(event)
        self.emitted += 1
        for sink in self._sinks:
            try:
                sink(event)
            except Exception:  # noqa: BLE001 - a sink must never break governance
                self.sink_errors += 1

    def recent(
        self,
        *,
        provider: str | None = None,
        code: EventCode | None = None,
        min_severity: Severity = Severity.INFO,
    ) -> list[GovernorEvent]:
        """Recent events, newest last, optionally filtered."""
        out: Iterable[GovernorEvent] = self._ring
        return [
            e
            for e in out
            if e.severity >= min_severity
            and (provider is None or e.provider == provider)
            and (code is None or e.code == code)
        ]

    def clear(self) -> None:
        """Drop the in-memory tail (sinks are untouched)."""
        self._ring.clear()


__all__ = [
    "EventCode",
    "EventSink",
    "GovernorEvent",
    "GovernorEventBus",
    "Severity",
]
