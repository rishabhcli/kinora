"""Render-trace timeline — reconstruct a shot's lifecycle from emitted spans.

When debugging "why did this shot take 40 s / fall to Ken-Burns / error?", an
operator wants the *whole* trace for one shot laid out in order: the scheduler
decision that promoted it, each §9.7 pipeline state, every provider call, and the
persist — with durations, parent/child nesting, and where it errored.

This module reconstructs that view from the flat list of finished
:class:`~app.telemetry.spans.SpanData` records an
:class:`~app.telemetry.exporters.InMemorySpanExporter` collected (the default
dependency-free sink). It builds the span tree (by ``parent_id``), computes a
critical path, and renders a JSON-friendly timeline the trace-read endpoint and
the demo debug panel can display — with **no** external tracing backend.

It is pure: give it spans, get a model back. Nothing here scrapes a collector or
calls a model.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from app.observability.facade import ATTR_SHOT
from app.telemetry.spans import STATUS_ERROR, SpanData

#: Span attribute key carrying the shot id (set by the observability facade).
SHOT_ATTR = ATTR_SHOT


@dataclass(slots=True)
class TimelineNode:
    """One span in a reconstructed timeline, with its children resolved."""

    span: SpanData
    children: list[TimelineNode] = field(default_factory=list)
    #: Offset of this span's start from the trace root start, in milliseconds.
    offset_ms: float = 0.0

    @property
    def name(self) -> str:
        return self.span.name

    @property
    def duration_ms(self) -> float:
        return round(self.span.duration_s * 1000, 3)

    @property
    def errored(self) -> bool:
        return self.span.status == STATUS_ERROR

    def walk(self) -> Iterable[TimelineNode]:
        """Yield this node then all descendants (pre-order)."""
        yield self
        for child in self.children:
            yield from child.walk()

    def to_dict(self) -> dict[str, Any]:
        """A nested JSON-friendly view (for the debug panel / trace endpoint)."""
        return {
            "name": self.name,
            "span_id": self.span.span_id,
            "parent_id": self.span.parent_id,
            "offset_ms": round(self.offset_ms, 3),
            "duration_ms": self.duration_ms,
            "status": self.span.status,
            "attributes": dict(self.span.attributes),
            "events": list(self.span.events),
            "children": [c.to_dict() for c in self.children],
        }


@dataclass(slots=True)
class RenderTimeline:
    """A reconstructed lifecycle for one trace (typically one shot render).

    Holds the forest of root nodes (a well-formed render has one root, but a
    truncated ring buffer or a cross-process hand-off can leave orphans, which are
    promoted to roots so nothing is dropped).
    """

    trace_id: str
    roots: list[TimelineNode] = field(default_factory=list)
    shot_id: str | None = None

    @property
    def span_count(self) -> int:
        return sum(1 for _ in self.iter_nodes())

    @property
    def total_duration_ms(self) -> float:
        """Wall-clock span of the trace: latest end minus earliest start."""
        starts = [n.span.start_s for n in self.iter_nodes()]
        ends = [n.span.end_s for n in self.iter_nodes() if n.span.end_s is not None]
        if not starts or not ends:
            return 0.0
        return round((max(ends) - min(starts)) * 1000, 3)

    @property
    def errored(self) -> bool:
        """True when any span in the trace errored."""
        return any(n.errored for n in self.iter_nodes())

    @property
    def error_spans(self) -> list[SpanData]:
        """Every errored span, in start order (the debugging short-list)."""
        errs = [n.span for n in self.iter_nodes() if n.errored]
        return sorted(errs, key=lambda s: s.start_s)

    def iter_nodes(self) -> Iterable[TimelineNode]:
        """Yield every node in the forest (pre-order, roots in start order)."""
        for root in self.roots:
            yield from root.walk()

    def critical_path(self) -> list[TimelineNode]:
        """The chain of longest-duration children from the slowest root.

        A quick "where did the wall-clock go?" answer: from the longest root,
        repeatedly descend into the longest child. Returns the node chain.
        """
        if not self.roots:
            return []
        node = max(self.roots, key=lambda n: n.span.duration_s)
        path = [node]
        while node.children:
            node = max(node.children, key=lambda c: c.span.duration_s)
            path.append(node)
        return path

    def spans_by_name(self, name: str) -> list[SpanData]:
        """Every span whose name matches (e.g. all ``provider.*`` calls)."""
        return [n.span for n in self.iter_nodes() if n.span.name == name]

    def to_dict(self) -> dict[str, Any]:
        """A JSON-friendly summary + nested forest for the debug surface."""
        return {
            "trace_id": self.trace_id,
            "shot_id": self.shot_id,
            "span_count": self.span_count,
            "total_duration_ms": self.total_duration_ms,
            "errored": self.errored,
            "roots": [r.to_dict() for r in self.roots],
        }


def _infer_shot_id(spans: Sequence[SpanData]) -> str | None:
    """Pull the shot id from the first span carrying the shot attribute."""
    for s in spans:
        value = s.attributes.get(SHOT_ATTR)
        if value is not None:
            return str(value)
    return None


def build_timeline(spans: Sequence[SpanData], *, trace_id: str | None = None) -> RenderTimeline:
    """Reconstruct a :class:`RenderTimeline` from a flat list of finished spans.

    When ``trace_id`` is given, only spans from that trace are used; otherwise the
    single trace present is assumed (mixed traces are still grouped by their own
    ``parent_id`` links, so an unknown parent simply makes that span a root).

    The forest is built by resolving each span's ``parent_id`` to a parent in the
    set; spans whose parent is absent become roots (so a ring-buffer eviction or a
    cross-process root can never drop the rest of the tree). Children and roots are
    ordered by start time, and ``offset_ms`` is filled relative to the earliest
    start so the timeline reads left-to-right.
    """
    selected = [s for s in spans if trace_id is None or s.trace_id == trace_id]
    resolved_trace = trace_id or (selected[0].trace_id if selected else "")

    nodes: dict[str, TimelineNode] = {s.span_id: TimelineNode(span=s) for s in selected}
    present = set(nodes)

    roots: list[TimelineNode] = []
    for node in nodes.values():
        parent_id = node.span.parent_id
        if parent_id is not None and parent_id in present:
            nodes[parent_id].children.append(node)
        else:
            roots.append(node)

    # Order roots and each node's children by start time for a readable timeline.
    roots.sort(key=lambda n: n.span.start_s)
    for node in nodes.values():
        node.children.sort(key=lambda c: c.span.start_s)

    # Fill start offsets relative to the earliest span in the trace.
    if nodes:
        origin = min(n.span.start_s for n in nodes.values())
        for node in nodes.values():
            node.offset_ms = (node.span.start_s - origin) * 1000

    return RenderTimeline(
        trace_id=resolved_trace,
        roots=roots,
        shot_id=_infer_shot_id(selected),
    )


def timelines_by_shot(spans: Sequence[SpanData]) -> dict[str, RenderTimeline]:
    """Group spans by shot id and reconstruct one timeline per shot.

    Convenience for the debug panel: a render worker's in-memory ring holds spans
    for many shots; this slices them per ``shot_id`` (spans with no shot id are
    ignored) and builds a timeline for each, keyed by shot id.
    """
    by_shot: dict[str, list[SpanData]] = {}
    for s in spans:
        sid = s.attributes.get(SHOT_ATTR)
        if sid is not None:
            by_shot.setdefault(str(sid), []).append(s)
    out: dict[str, RenderTimeline] = {}
    for sid, shot_spans in by_shot.items():
        # Group by the dominant trace id so two renders of the same shot id don't
        # merge into one tangled tree; pick the trace with the most spans.
        trace_counts: dict[str, int] = {}
        for sp in shot_spans:
            trace_counts[sp.trace_id] = trace_counts.get(sp.trace_id, 0) + 1
        dominant = max(trace_counts, key=lambda t: trace_counts[t])
        out[sid] = build_timeline(shot_spans, trace_id=dominant)
    return out


__all__ = [
    "SHOT_ATTR",
    "RenderTimeline",
    "TimelineNode",
    "build_timeline",
    "timelines_by_shot",
]
