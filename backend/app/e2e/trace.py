"""Golden-trace recording: a scenario's observable event sequence, regression-checked.

A scenario driving the real flow emits an ordered stream of *observable* events
(a shot reaches accepted, a page turns, the buffer crosses a watermark, a
provider fails over, the budget exhausts). :class:`TraceRecorder` captures that
stream into a canonical, JSON-stable :class:`GoldenTrace` so a test can assert
the whole shape against a checked-in golden — the single regression signal that
"the book still becomes the same page-synced film".

The trace is deterministic by construction: events carry only stable fields
(never wall-clock times, never object ids), floats are rounded, and the virtual
clock supplies any timestamps. ``GoldenTrace.canonical()`` is the byte-stable
form a test diffs / stores.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from app.core.logging import get_logger

logger = get_logger("app.e2e.trace")

#: Rounding for any float that enters a trace, so ffmpeg/float jitter never
#: perturbs a golden (durations land on frame-ish boundaries, ETAs are coarse).
_FLOAT_NDIGITS = 3


def _canonicalize(value: Any) -> Any:
    """Recursively round floats + sort dict keys so a trace is byte-stable."""
    if isinstance(value, float):
        return round(value, _FLOAT_NDIGITS)
    if isinstance(value, dict):
        return {k: _canonicalize(value[k]) for k in sorted(value)}
    if isinstance(value, (list, tuple)):
        return [_canonicalize(v) for v in value]
    return value


@dataclass(frozen=True, slots=True)
class TraceEvent:
    """One observable step in a scenario (kind + canonicalized payload)."""

    seq: int
    kind: str
    data: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {"seq": self.seq, "kind": self.kind, "data": _canonicalize(self.data)}


@dataclass
class GoldenTrace:
    """An ordered, JSON-stable sequence of :class:`TraceEvent`."""

    events: list[TraceEvent] = field(default_factory=list)

    def kinds(self) -> list[str]:
        """Just the event kinds, in order (the coarsest regression signal)."""
        return [e.kind for e in self.events]

    def of_kind(self, kind: str) -> list[TraceEvent]:
        return [e for e in self.events if e.kind == kind]

    def as_list(self) -> list[dict[str, Any]]:
        return [e.as_dict() for e in self.events]

    def canonical(self) -> str:
        """The byte-stable JSON form a golden test diffs / stores."""
        return json.dumps(self.as_list(), sort_keys=True, separators=(",", ":"))

    def __len__(self) -> int:
        return len(self.events)


class TraceRecorder:
    """Append-only recorder; ``record(kind, **data)`` adds the next event."""

    def __init__(self) -> None:
        self._events: list[TraceEvent] = []

    def record(self, kind: str, /, **data: Any) -> TraceEvent:
        event = TraceEvent(seq=len(self._events), kind=kind, data=data)
        self._events.append(event)
        logger.debug("e2e.trace", kind=kind)
        return event

    def trace(self) -> GoldenTrace:
        return GoldenTrace(events=list(self._events))


__all__ = ["GoldenTrace", "TraceEvent", "TraceRecorder"]
