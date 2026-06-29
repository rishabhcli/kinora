"""The decision log / audit sink — an append-only record of every decision.

Every ``check`` the plane resolves can be recorded as a :class:`DecisionRecord`:
the request (subject / action / resource / context), the verdict, the full reason
trail, whether it was a cache hit, and a timestamp. This is the *audit* half of
the plane: "who was allowed/denied what, and why" — the thing a unified plane
gives you that scattered checks never could.

The sink is a protocol (:class:`DecisionLog`) so a deployment chooses the
destination: an in-memory ring buffer (default, for tests + introspection), a
structured-logging sink, or a DB-backed table (``authz_decision_log``). The SDK
holds one sink and writes a record per decision (sampling is the deployment's
choice; the default logs everything but only keeps the most recent ``capacity``
in memory).

Records are immutable and carry a stable ``digest`` (a hash over the decisive
fields) so duplicate decisions can be coalesced in analytics and so a tamper-
evident chain can be built on top if needed.
"""

from __future__ import annotations

import hashlib
from collections import deque
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from app.platform.authz.model import Decision, Effect, Reason


@dataclass(frozen=True, slots=True)
class DecisionRecord:
    """One audited authorization decision (immutable)."""

    subject_ref: str
    action: str
    resource_ref: str
    effect: Effect
    reasons: tuple[str, ...]
    cached: bool
    evaluated_at: datetime
    context: tuple[tuple[str, str], ...] = ()

    @property
    def allowed(self) -> bool:
        return self.effect is Effect.ALLOW

    @property
    def digest(self) -> str:
        """A stable hash over the decisive fields (for coalescing / chaining)."""
        payload = (
            f"{self.subject_ref}|{self.action}|{self.resource_ref}|{self.effect.value}"
        )
        return hashlib.sha256(payload.encode()).hexdigest()[:16]

    @classmethod
    def from_decision(cls, decision: Decision) -> DecisionRecord:
        req = decision.request
        return cls(
            subject_ref=req.subject.ref,
            action=req.action,
            resource_ref=req.resource.ref,
            effect=decision.effect,
            reasons=tuple(_render(r) for r in decision.reasons),
            cached=decision.cached,
            evaluated_at=decision.evaluated_at,
            context=tuple(
                (str(k), str(v))
                for k, v in sorted(req.context.attributes.items())
                if k != "now"
            ),
        )

    def render(self) -> str:
        """A one-line log rendering."""
        flag = " (cached)" if self.cached else ""
        return (
            f"{self.evaluated_at.isoformat()} {self.effect.value.upper()}{flag} "
            f"{self.subject_ref} {self.action} {self.resource_ref}"
        )


def _render(reason: Reason) -> str:
    return reason.render()


class DecisionLog(Protocol):
    """The append-only sink the SDK writes decisions to."""

    def record(self, decision: Decision) -> None:
        """Append a record for ``decision``."""
        ...


class InMemoryDecisionLog:
    """A bounded ring buffer of recent decisions (default sink; introspectable).

    Keeps the most recent ``capacity`` records. Exposes filtered queries so a
    test or an admin endpoint can ask "every denial for this subject".
    """

    def __init__(self, *, capacity: int = 10_000) -> None:
        self._records: deque[DecisionRecord] = deque(maxlen=capacity)

    def record(self, decision: Decision) -> None:
        self._records.append(DecisionRecord.from_decision(decision))

    def __len__(self) -> int:
        return len(self._records)

    def __iter__(self) -> Iterator[DecisionRecord]:
        return iter(self._records)

    @property
    def records(self) -> tuple[DecisionRecord, ...]:
        return tuple(self._records)

    def for_subject(self, subject_ref: str) -> list[DecisionRecord]:
        return [r for r in self._records if r.subject_ref == subject_ref]

    def for_resource(self, resource_ref: str) -> list[DecisionRecord]:
        return [r for r in self._records if r.resource_ref == resource_ref]

    def denials(self) -> list[DecisionRecord]:
        return [r for r in self._records if not r.allowed]

    def clear(self) -> None:
        self._records.clear()


class NullDecisionLog:
    """A sink that records nothing (audit disabled)."""

    def record(self, decision: Decision) -> None:
        return None


class CompositeDecisionLog:
    """Fan a decision out to several sinks (e.g. in-memory + DB)."""

    def __init__(self, sinks: Iterable[DecisionLog] = ()) -> None:
        self._sinks: list[DecisionLog] = list(sinks)

    def add(self, sink: DecisionLog) -> None:
        self._sinks.append(sink)

    def record(self, decision: Decision) -> None:
        for sink in self._sinks:
            sink.record(decision)


# Optional aggregation helpers (analytics over the log) --------------------- #


@dataclass
class DecisionStats:
    """Aggregate counts over a slice of the decision log."""

    total: int = 0
    allowed: int = 0
    denied: int = 0
    cached: int = 0
    by_action: dict[str, int] = field(default_factory=dict)

    @property
    def deny_rate(self) -> float:
        return self.denied / self.total if self.total else 0.0


def summarize(records: Iterable[DecisionRecord]) -> DecisionStats:
    """Aggregate a decision-log slice into :class:`DecisionStats`."""
    stats = DecisionStats()
    for r in records:
        stats.total += 1
        if r.allowed:
            stats.allowed += 1
        else:
            stats.denied += 1
        if r.cached:
            stats.cached += 1
        stats.by_action[r.action] = stats.by_action.get(r.action, 0) + 1
    return stats


__all__ = [
    "CompositeDecisionLog",
    "DecisionLog",
    "DecisionRecord",
    "DecisionStats",
    "InMemoryDecisionLog",
    "NullDecisionLog",
    "summarize",
]
