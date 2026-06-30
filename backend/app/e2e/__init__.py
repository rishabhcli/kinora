"""Deterministic, infra-free end-to-end harness for the Kinora flow (the integration safety net)."""

from __future__ import annotations

from app.e2e.assertions import (
    HarnessAssertionError,
    assert_buffer_ahead_of_reader,
    assert_no_double_spend,
    assert_shots_accepted,
    assert_sync_covers_narration,
)
from app.e2e.clock import VirtualClock
from app.e2e.scenarios import (
    ScenarioOutcome,
    budget_exhausts_mid_read,
    director_comments_on_region,
    ingest_synthetic_book,
    live_gate_off_degrades,
    provider_fails_over,
    reader_opens_and_turns_pages,
)
from app.e2e.synthetic_book import SyntheticBook, make_synthetic_book
from app.e2e.trace import GoldenTrace, TraceEvent, TraceRecorder
from app.e2e.world import FakeWorld, WorldConfig

__all__ = [
    "FakeWorld",
    "GoldenTrace",
    "HarnessAssertionError",
    "ScenarioOutcome",
    "SyntheticBook",
    "TraceEvent",
    "TraceRecorder",
    "VirtualClock",
    "WorldConfig",
    "assert_buffer_ahead_of_reader",
    "assert_no_double_spend",
    "assert_shots_accepted",
    "assert_sync_covers_narration",
    "budget_exhausts_mid_read",
    "director_comments_on_region",
    "ingest_synthetic_book",
    "live_gate_off_degrades",
    "make_synthetic_book",
    "provider_fails_over",
    "reader_opens_and_turns_pages",
]
