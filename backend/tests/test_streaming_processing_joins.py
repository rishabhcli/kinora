"""Join tests: stream-stream interval join and stream-table enrichment join."""

from __future__ import annotations

from typing import Any

from app.streaming.processing.datastream import StreamEnvironment
from app.streaming.processing.joins import IntervalJoinOperator, TaggedRecord
from app.streaming.processing.testkit import TestHarness
from app.streaming.processing.time_domain import (
    WatermarkStrategy,
    field_timestamp_assigner,
)


def test_interval_join_operator_matches_within_window() -> None:
    op: IntervalJoinOperator = IntervalJoinOperator(
        lower_ms=0, upper_ms=100, join_fn=lambda left, right: (left, right)
    )
    h: TestHarness = TestHarness(op)
    # left at t=1000, right at t=1050 -> within [1000, 1100], should join
    h.process_value(TaggedRecord(is_left=True, left="L"), timestamp=1_000, key="k")
    h.process_value(TaggedRecord(is_left=False, right="R"), timestamp=1_050, key="k")
    assert ("L", "R") in h.output_values()


def test_interval_join_operator_skips_out_of_window() -> None:
    op: IntervalJoinOperator = IntervalJoinOperator(
        lower_ms=0, upper_ms=100, join_fn=lambda left, right: (left, right)
    )
    h: TestHarness = TestHarness(op)
    h.process_value(TaggedRecord(is_left=True, left="L"), timestamp=1_000, key="k")
    # right too late (t=1200 > 1000+100)
    h.process_value(TaggedRecord(is_left=False, right="R"), timestamp=1_200, key="k")
    assert h.output_values() == []


def test_interval_join_evicts_on_watermark() -> None:
    op: IntervalJoinOperator = IntervalJoinOperator(
        lower_ms=0, upper_ms=100, join_fn=lambda left, right: (left, right)
    )
    h: TestHarness = TestHarness(op)
    h.process_value(TaggedRecord(is_left=True, left="L"), timestamp=1_000, key="k")
    # advance watermark past 1000+100 -> left buffer evicted
    h.process_watermark(1_200)
    # a late right that would have matched no longer finds the evicted left
    h.process_value(TaggedRecord(is_left=False, right="R"), timestamp=1_050, key="k")
    assert h.output_values() == []


def test_end_to_end_interval_join_pipeline() -> None:
    """Two keyed streams joined through the full runtime two-input path."""

    env = StreamEnvironment()

    def wm() -> WatermarkStrategy[dict]:
        return WatermarkStrategy.for_bounded_out_of_orderness(
            field_timestamp_assigner(lambda v: int(v["ts"])), 0
        )

    left = (
        env.from_source(
            [{"id": "a", "ts": 1_000, "side": "req"}], name="left"
        )
        .assign_timestamps_and_watermarks(wm())
        .key_by(lambda v: v["id"])
    )
    right = (
        env.from_source(
            [{"id": "a", "ts": 1_030, "side": "clip"}], name="right"
        )
        .assign_timestamps_and_watermarks(wm())
        .key_by(lambda v: v["id"])
    )
    def latency(lft: dict[str, Any], rgt: dict[str, Any]) -> int:
        return int(rgt["ts"]) - int(lft["ts"])

    joined = left.interval_join(right, lower_ms=0, upper_ms=100, join_fn=latency)
    result = env.execute()
    assert 30 in result.values(joined.node_id)


def test_end_to_end_stream_table_join() -> None:
    """A fact stream enriched with a slowly-changing dimension table."""

    env = StreamEnvironment()

    def wm() -> WatermarkStrategy[dict]:
        return WatermarkStrategy.for_bounded_out_of_orderness(
            field_timestamp_assigner(lambda v: int(v["ts"])), 0
        )

    facts = (
        env.from_source(
            [
                {"book": "b1", "ts": 100, "shot": "s1"},
                {"book": "b1", "ts": 200, "shot": "s2"},
            ],
            name="facts",
        )
        .assign_timestamps_and_watermarks(wm())
        .key_by(lambda v: v["book"])
    )
    dim = (
        env.from_source([{"book": "b1", "ts": 0, "title": "Snow White"}], name="dim")
        .assign_timestamps_and_watermarks(wm())
        .key_by(lambda v: v["book"])
    )
    def enrich(fact: dict[str, Any], table: dict[str, Any] | None) -> dict[str, Any]:
        return {**fact, "title": table["title"] if table else None}

    enriched = facts.join_table(
        dim,
        join_fn=enrich,
        table_key_selector=lambda d: d["book"],
    )
    result = env.execute()
    titles = {r["title"] for r in result.typed_values(enriched.node_id, dict)}
    assert titles == {"Snow White"}
