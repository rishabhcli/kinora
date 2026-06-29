"""Stateful + property tests for the §4.11/§12.1 poison tracker.

The poison tracker is a small state machine over a shot's hard-failure history:
failures accrue (permanent weigh double), crossing a threshold quarantines the
shot (forcing the guaranteed-renderable bottom rung), and a clean success clears
the history. A :class:`RuleBasedStateMachine` fires random failure/success/probe
commands and asserts the tracker's observable state against an independent
counter model — so the "quarantine is monotone until a success clears it" and
"quarantine forces the audio card" guarantees are proven over command sequences.
"""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, rule

from app.render.ladder import LadderReason, Rung
from app.render.poison import InMemoryPoisonStore, PoisonRecord, PoisonTracker

# A spread of exception types: permanent (weight 2) vs transient (weight 1).
PERMANENT = [ValueError("x"), LookupError("x"), TypeError("x")]
TRANSIENT = [RuntimeError("x"), TimeoutError("x"), ConnectionError("x")]
exceptions = st.sampled_from(PERMANENT + TRANSIENT)


def _weight(exc: BaseException) -> int:
    return 2 if isinstance(exc, ValueError | LookupError | TypeError) else 1


class PoisonModel(RuleBasedStateMachine):
    """Drive a PoisonTracker against an independent weighted-failure counter."""

    def __init__(self) -> None:
        super().__init__()
        self.tracker = PoisonTracker(store=InMemoryPoisonStore(), threshold=3)
        self.model_failures = 0
        self.model_quarantined = False

    @rule(exc=exceptions)
    def fail(self, exc: BaseException) -> None:
        self.model_failures += _weight(exc)
        if self.model_failures >= self.tracker.threshold:
            self.model_quarantined = True
        record = self.tracker.record_failure("shot", exc)
        assert record.failures == self.model_failures
        assert record.quarantined == self.model_quarantined

    @rule()
    def succeed(self) -> None:
        self.tracker.record_success("shot")
        self.model_failures = 0
        self.model_quarantined = False

    @invariant()
    def tracker_agrees_on_failures(self) -> None:
        assert self.tracker.failures("shot") == self.model_failures

    @invariant()
    def tracker_agrees_on_quarantine(self) -> None:
        assert self.tracker.is_poisoned("shot") == self.model_quarantined

    @invariant()
    def quarantine_implies_forced_bottom_rung(self) -> None:
        forced = self.tracker.quarantine_plan_input("shot")
        if self.model_quarantined:
            assert forced == (Rung.AUDIO_TEXT_ONLY, LadderReason.POISONED)
        else:
            assert forced is None


TestPoisonModel = PoisonModel.TestCase


# --------------------------------------------------------------------------- #
# Direct property tests
# --------------------------------------------------------------------------- #


@given(st.lists(exceptions, max_size=10))
def test_failures_never_decrease_without_success(excs: list[BaseException]) -> None:
    """Failure count is monotone non-decreasing across a run of failures."""
    tracker = PoisonTracker(store=InMemoryPoisonStore(), threshold=3)
    prev = 0
    for exc in excs:
        tracker.record_failure("s", exc)
        now = tracker.failures("s")
        assert now >= prev
        prev = now


@given(st.lists(exceptions, max_size=10))
def test_quarantine_is_sticky_until_success(excs: list[BaseException]) -> None:
    """Once quarantined, a shot stays quarantined until an explicit success clears it."""
    tracker = PoisonTracker(store=InMemoryPoisonStore(), threshold=3)
    became = False
    for exc in excs:
        tracker.record_failure("s", exc)
        if tracker.is_poisoned("s"):
            became = True
        # Sticky: once true, never flips back to false on further failures.
        assert tracker.is_poisoned("s") == became or not became


@given(st.lists(exceptions, min_size=2, max_size=8))
def test_success_always_clears(excs: list[BaseException]) -> None:
    """A clean success resets failures to 0 and lifts any quarantine."""
    tracker = PoisonTracker(store=InMemoryPoisonStore(), threshold=3)
    for exc in excs:
        tracker.record_failure("s", exc)
    tracker.record_success("s")
    assert tracker.failures("s") == 0
    assert not tracker.is_poisoned("s")
    assert tracker.quarantine_plan_input("s") is None


@given(st.integers(min_value=1, max_value=6))
def test_permanent_failures_quarantine_at_double_weight(threshold: int) -> None:
    """A permanent failure counts double — quarantine arrives in ⌈threshold/2⌉ hits."""
    import math

    tracker = PoisonTracker(store=InMemoryPoisonStore(), threshold=threshold)
    needed = math.ceil(threshold / 2)
    for i in range(needed):
        tracker.record_failure("s", ValueError("boom"))
        # Before the last needed hit, not yet at threshold (2*(i+1) < threshold).
        if i + 1 < needed and 2 * (i + 1) < threshold:
            assert not tracker.is_poisoned("s")
    assert tracker.is_poisoned("s")


@given(st.text(min_size=1, max_size=8), st.integers(0, 9), st.booleans())
def test_record_roundtrips_through_dict(
    shot_id: str, failures: int, quarantined: bool
) -> None:
    """``PoisonRecord`` serialization is a faithful round-trip (durable store contract)."""
    record = PoisonRecord(
        shot_id=shot_id, failures=failures, last_error="X", quarantined=quarantined
    )
    restored = PoisonRecord.from_dict(record.as_dict())
    assert restored == record


def test_store_returns_a_copy_not_the_live_record() -> None:
    """The in-memory store hands back copies so a caller can't mutate its state."""
    store = InMemoryPoisonStore()
    store.put(PoisonRecord(shot_id="s", failures=1))
    got = store.get("s")
    assert got is not None
    got.failures = 999  # mutate the copy
    again = store.get("s")
    assert again is not None
    assert again.failures == 1  # the stored record is untouched
