"""Dead-shot / poison handling for the render engine (kinora.md §4.11, §12.1).

Hard-failure counting, permanent-double-weight, quarantine at the threshold, the
forced bottom-rung plan input, success clearing, the telemetry event, and store
serialisation. No DB/ffmpeg.
"""

from __future__ import annotations

from app.observability import metrics
from app.render.ladder import LadderReason, Rung
from app.render.pipeline import UnknownShotError
from app.render.poison import (
    InMemoryPoisonStore,
    PoisonRecord,
    PoisonTracker,
)
from app.render.telemetry import EventKind, recording_bus


def test_transient_failures_accumulate_to_quarantine() -> None:
    tracker = PoisonTracker(threshold=3)
    for _ in range(2):
        tracker.record_failure("s", RuntimeError("blip"))
    assert not tracker.is_poisoned("s")
    assert tracker.failures("s") == 2
    tracker.record_failure("s", RuntimeError("blip"))
    assert tracker.is_poisoned("s")


def test_permanent_failure_counts_double_weight() -> None:
    tracker = PoisonTracker(threshold=3)
    # A deterministically-broken shot poisons in 2 hits (weight 2 each ≥ 3 → on the 2nd).
    tracker.record_failure("s", UnknownShotError("gone"))  # +2
    assert not tracker.is_poisoned("s")  # 2 < 3
    tracker.record_failure("s", UnknownShotError("gone"))  # +2 → 4 ≥ 3
    assert tracker.is_poisoned("s")
    assert tracker.failures("s") == 4


def test_quarantine_forces_bottom_rung() -> None:
    tracker = PoisonTracker(threshold=1)
    assert tracker.quarantine_plan_input("s") is None  # clean shot
    tracker.record_failure("s", RuntimeError("blip"))
    plan_input = tracker.quarantine_plan_input("s")
    assert plan_input == (Rung.AUDIO_TEXT_ONLY, LadderReason.POISONED)


def test_success_clears_poison_history() -> None:
    tracker = PoisonTracker(threshold=3)
    tracker.record_failure("s", RuntimeError("blip"))
    assert tracker.failures("s") == 1
    tracker.record_success("s")
    assert tracker.failures("s") == 0
    assert not tracker.is_poisoned("s")


def test_quarantine_emits_telemetry_and_metric_once() -> None:
    bus, recorder = recording_bus()
    tracker = PoisonTracker(threshold=2, bus=bus)
    before = metrics.render_poison_total._value.get()
    tracker.record_failure("s", RuntimeError("blip"))  # 1 — not yet
    tracker.record_failure("s", RuntimeError("blip"))  # 2 — quarantine now
    tracker.record_failure("s", RuntimeError("blip"))  # 3 — already poisoned
    # The poisoned event + metric fire exactly once, on the transition.
    assert recorder.count(EventKind.POISONED) == 1
    assert metrics.render_poison_total._value.get() == before + 1
    event = recorder.events(kind=EventKind.POISONED)[0]
    assert event.data["failures"] == 2


def test_store_returns_copies_not_live_records() -> None:
    store = InMemoryPoisonStore()
    store.put(PoisonRecord(shot_id="s", failures=1))
    got = store.get("s")
    assert got is not None
    got.failures = 999  # mutate the copy
    again = store.get("s")
    assert again is not None and again.failures == 1  # store unaffected


def test_record_serialisation_roundtrips() -> None:
    rec = PoisonRecord(shot_id="s", failures=4, last_error="RuntimeError", quarantined=True)
    restored = PoisonRecord.from_dict(rec.as_dict())
    assert restored == rec


def test_independent_shots_tracked_separately() -> None:
    tracker = PoisonTracker(threshold=2)
    tracker.record_failure("a", RuntimeError("x"))
    tracker.record_failure("a", RuntimeError("x"))
    tracker.record_failure("b", RuntimeError("x"))
    assert tracker.is_poisoned("a")
    assert not tracker.is_poisoned("b")
