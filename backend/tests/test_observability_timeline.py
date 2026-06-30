"""Observability render-trace timeline — reconstruct a shot's lifecycle from spans.

The timeline reader takes the flat list of finished spans an in-memory exporter
collected and rebuilds the tree (by ``parent_id``), the critical path, the error
short-list, and per-shot grouping — all offline, no tracing backend.
"""

from __future__ import annotations

import time

from app.observability.facade import ATTR_SHOT
from app.observability.timeline import build_timeline, timelines_by_shot
from app.telemetry.spans import STATUS_ERROR, STATUS_OK, SpanData

T = "a" * 32  # a single trace id for the well-formed cases


def _span(
    name: str,
    span_id: str,
    parent_id: str | None,
    *,
    start: float,
    dur: float,
    trace_id: str = T,
    status: str = STATUS_OK,
    shot: str | None = None,
) -> SpanData:
    attrs: dict[str, object] = {}
    if shot is not None:
        attrs[ATTR_SHOT] = shot
    return SpanData(
        name=name,
        trace_id=trace_id,
        span_id=span_id.ljust(16, "0"),
        parent_id=parent_id.ljust(16, "0") if parent_id else None,
        start_s=start,
        end_s=start + dur,
        status=status,
        attributes=attrs,
    )


def test_build_timeline_reconstructs_the_tree() -> None:
    spans = [
        _span("render.shot", "01", None, start=0.0, dur=10.0, shot="shot-1"),
        _span("provider.i2v", "02", "01", start=1.0, dur=6.0, shot="shot-1"),
        _span("persist", "03", "01", start=8.0, dur=1.0, shot="shot-1"),
    ]
    tl = build_timeline(spans)
    assert tl.span_count == 3
    assert tl.shot_id == "shot-1"
    assert len(tl.roots) == 1
    root = tl.roots[0]
    assert root.name == "render.shot"
    assert [c.name for c in root.children] == ["provider.i2v", "persist"]


def test_total_duration_is_wallclock_span() -> None:
    spans = [
        _span("root", "01", None, start=100.0, dur=10.0),
        _span("child", "02", "01", start=101.0, dur=2.0),
    ]
    tl = build_timeline(spans)
    # 100.0 -> 110.0 wall-clock.
    assert tl.total_duration_ms == 10000.0


def test_offsets_are_relative_to_earliest_start() -> None:
    spans = [
        _span("root", "01", None, start=5.0, dur=10.0),
        _span("child", "02", "01", start=7.5, dur=1.0),
    ]
    tl = build_timeline(spans)
    by_name = {n.name: n for n in tl.iter_nodes()}
    assert by_name["root"].offset_ms == 0.0
    assert by_name["child"].offset_ms == 2500.0


def test_orphan_span_is_promoted_to_a_root() -> None:
    # The parent was evicted from the ring; the child must not be dropped.
    spans = [
        _span("child", "02", "99", start=1.0, dur=1.0),
    ]
    tl = build_timeline(spans)
    assert tl.span_count == 1
    assert [r.name for r in tl.roots] == ["child"]


def test_critical_path_descends_longest_children() -> None:
    spans = [
        _span("root", "01", None, start=0.0, dur=10.0),
        _span("fast", "02", "01", start=0.0, dur=1.0),
        _span("slow", "03", "01", start=1.0, dur=8.0),
        _span("slow.leaf", "04", "03", start=1.0, dur=7.0),
    ]
    tl = build_timeline(spans)
    path = [n.name for n in tl.critical_path()]
    assert path == ["root", "slow", "slow.leaf"]


def test_errored_flag_and_error_span_shortlist() -> None:
    spans = [
        _span("root", "01", None, start=0.0, dur=5.0),
        _span("ok", "02", "01", start=0.0, dur=1.0),
        _span("bad", "03", "01", start=2.0, dur=1.0, status=STATUS_ERROR),
    ]
    tl = build_timeline(spans)
    assert tl.errored is True
    assert [s.name for s in tl.error_spans] == ["bad"]


def test_spans_by_name_filters() -> None:
    spans = [
        _span("root", "01", None, start=0.0, dur=5.0),
        _span("provider.i2v", "02", "01", start=0.0, dur=1.0),
        _span("provider.i2v", "03", "01", start=2.0, dur=1.0),
    ]
    tl = build_timeline(spans)
    assert len(tl.spans_by_name("provider.i2v")) == 2


def test_build_timeline_filters_by_trace_id() -> None:
    other = "b" * 32
    spans = [
        _span("root-a", "01", None, start=0.0, dur=1.0, trace_id=T),
        _span("root-b", "02", None, start=0.0, dur=1.0, trace_id=other),
    ]
    tl = build_timeline(spans, trace_id=T)
    assert tl.span_count == 1
    assert tl.roots[0].name == "root-a"


def test_timelines_by_shot_groups_per_shot() -> None:
    spans = [
        _span("render", "01", None, start=0.0, dur=2.0, shot="shot-A"),
        _span("provider", "02", "01", start=0.0, dur=1.0, shot="shot-A"),
        _span("render", "03", None, start=0.0, dur=2.0, trace_id="c" * 32, shot="shot-B"),
    ]
    by_shot = timelines_by_shot(spans)
    assert set(by_shot) == {"shot-A", "shot-B"}
    assert by_shot["shot-A"].span_count == 2
    assert by_shot["shot-B"].span_count == 1


def test_to_dict_is_json_friendly_and_nested() -> None:
    spans = [
        _span("root", "01", None, start=0.0, dur=2.0, shot="shot-1"),
        _span("child", "02", "01", start=0.5, dur=1.0, shot="shot-1"),
    ]
    tl = build_timeline(spans)
    d = tl.to_dict()
    assert d["trace_id"] == T
    assert d["shot_id"] == "shot-1"
    assert d["span_count"] == 2
    assert d["roots"][0]["name"] == "root"
    assert d["roots"][0]["children"][0]["name"] == "child"


def test_empty_spans_yields_empty_timeline() -> None:
    tl = build_timeline([])
    assert tl.span_count == 0
    assert tl.total_duration_ms == 0.0
    assert tl.critical_path() == []
    assert tl.errored is False


def test_open_span_without_end_does_not_break_duration() -> None:
    now = time.monotonic()
    s = SpanData(name="open", trace_id=T, span_id="01".ljust(16, "0"), parent_id=None, start_s=now)
    tl = build_timeline([s])
    # No end recorded → duration falls back to 0, never raises.
    assert tl.total_duration_ms == 0.0
    assert tl.span_count == 1
