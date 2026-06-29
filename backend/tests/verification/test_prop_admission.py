"""Property tests for the §12.2 admission-control decision (``decide_admission``).

The pure admission decision is the queue's backpressure + fairness policy, mirrored
so the Scheduler can pre-check. Its safety contract is sharp: committed and keyframe
lanes are *never* shed (the committed buffer must never stall, the keyframe ladder
must always be fillable); only the speculative lane is droppable, and only under
depth backpressure or the per-session cap.
"""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from app.db.models.enums import RenderPriority
from app.queue.admission import AdmissionReason, decide_admission

priorities = st.sampled_from(list(RenderPriority))
depths = st.integers(min_value=0, max_value=10_000)
caps = st.one_of(st.none(), st.integers(min_value=0, max_value=100))
inflight = st.integers(min_value=0, max_value=200)


@given(priorities, depths, depths, inflight, caps)
def test_decision_is_well_formed(
    priority: RenderPriority,
    total: int,
    backpressure: int,
    session_inflight: int,
    cap: int | None,
) -> None:
    """The decision always yields a bool + a reason whose admit-sense matches."""
    decision = decide_admission(
        priority=priority,
        total_depth=total,
        backpressure_depth=backpressure,
        session_inflight=session_inflight,
        session_cap=cap,
    )
    assert isinstance(decision.admit, bool)
    assert decision.reason in set(AdmissionReason)
    # __bool__ proxies admit.
    assert bool(decision) is decision.admit
    sheds = {AdmissionReason.SHED_BACKPRESSURE, AdmissionReason.SHED_SESSION_CAP}
    assert (decision.reason in sheds) == (not decision.admit)


@given(depths, depths, inflight, caps)
def test_committed_is_always_admitted(
    total: int, backpressure: int, session_inflight: int, cap: int | None
) -> None:
    """The committed lane never sheds — the buffer must never stall (§12.2)."""
    decision = decide_admission(
        priority=RenderPriority.COMMITTED,
        total_depth=total,
        backpressure_depth=backpressure,
        session_inflight=session_inflight,
        session_cap=cap,
    )
    assert decision.admit
    assert decision.reason is AdmissionReason.ADMIT_COMMITTED


@given(depths, depths, inflight, caps)
def test_keyframe_is_always_admitted(
    total: int, backpressure: int, session_inflight: int, cap: int | None
) -> None:
    """The keyframe lane never sheds — the cheap image ladder is always fillable."""
    decision = decide_admission(
        priority=RenderPriority.KEYFRAME,
        total_depth=total,
        backpressure_depth=backpressure,
        session_inflight=session_inflight,
        session_cap=cap,
    )
    assert decision.admit
    assert decision.reason is AdmissionReason.ADMIT_KEYFRAME


@given(depths, depths, inflight, caps)
def test_only_speculative_can_be_shed(
    total: int, backpressure: int, session_inflight: int, cap: int | None
) -> None:
    """A shed decision implies the priority was speculative."""
    for priority in RenderPriority:
        decision = decide_admission(
            priority=priority,
            total_depth=total,
            backpressure_depth=backpressure,
            session_inflight=session_inflight,
            session_cap=cap,
        )
        if not decision.admit:
            assert priority is RenderPriority.SPECULATIVE


@given(depths, depths)
def test_speculative_backpressure_threshold(total: int, backpressure: int) -> None:
    """Speculative is shed exactly when total depth ≥ the backpressure threshold."""
    decision = decide_admission(
        priority=RenderPriority.SPECULATIVE,
        total_depth=total,
        backpressure_depth=backpressure,
        session_cap=None,
    )
    if total >= backpressure:
        assert not decision.admit
        assert decision.reason is AdmissionReason.SHED_BACKPRESSURE
    else:
        assert decision.admit
        assert decision.reason is AdmissionReason.ADMIT_UNDER_LIMITS


@given(st.integers(0, 100), st.integers(0, 100))
def test_session_cap_sheds_when_at_or_over(inflight_n: int, cap: int) -> None:
    """Under depth limits, the session cap sheds when in-flight ≥ cap."""
    # total_depth < backpressure_depth so backpressure never fires.
    decision = decide_admission(
        priority=RenderPriority.SPECULATIVE,
        total_depth=0,
        backpressure_depth=10_000,
        session_inflight=inflight_n,
        session_cap=cap,
    )
    if inflight_n >= cap:
        assert not decision.admit
        assert decision.reason is AdmissionReason.SHED_SESSION_CAP
    else:
        assert decision.admit


@given(depths, depths, inflight, caps)
def test_backpressure_precedes_session_cap(
    total: int, backpressure: int, session_inflight: int, cap: int | None
) -> None:
    """When both would shed, backpressure is reported first (the documented order)."""
    decision = decide_admission(
        priority=RenderPriority.SPECULATIVE,
        total_depth=total,
        backpressure_depth=backpressure,
        session_inflight=session_inflight,
        session_cap=cap,
    )
    over_depth = total >= backpressure
    over_cap = cap is not None and session_inflight >= cap
    if over_depth:
        assert decision.reason is AdmissionReason.SHED_BACKPRESSURE
    elif over_cap:
        assert decision.reason is AdmissionReason.SHED_SESSION_CAP


@given(depths, depths, inflight, caps)
def test_decision_is_deterministic(
    total: int, backpressure: int, session_inflight: int, cap: int | None
) -> None:
    def call() -> object:
        return decide_admission(
            priority=RenderPriority.SPECULATIVE,
            total_depth=total,
            backpressure_depth=backpressure,
            session_inflight=session_inflight,
            session_cap=cap,
        )

    assert call() == call()
