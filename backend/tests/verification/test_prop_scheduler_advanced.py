"""Property tests for the advanced scheduler policy: adaptive watermarks + fairness.

Two pure surfaces that shape *when* and *how much* the scheduler buffers, both of
which carry a strict safety contract:

* :func:`adapt_watermarks` — widens the §4.5 band for a noisy/fast reader but is
  clamped so it can only make the buffer *deeper* (never thinner) and always
  preserves the ``base <= value``, ``L < C < H``, ``H - L >= min_band`` invariants.
* :class:`FairShareAllocator` — splits a shared video-second pool across draining
  sessions. Its spend invariant (total allocated ≤ pool), feasibility (no session
  over its deficit/sub-cap), and zero-allocation-for-non-needy laws are what stop
  one reader from starving the others while keeping the budget honest.
"""

from __future__ import annotations

from hypothesis import assume, given
from hypothesis import strategies as st

from app.scheduler.adaptive import AdaptiveConfig, Watermarks, adapt_watermarks
from app.scheduler.fairness import FairShareAllocator, SessionDemand
from app.scheduler.prediction import ReadingModel

# --------------------------------------------------------------------------- #
# adapt_watermarks
# --------------------------------------------------------------------------- #

#: A base watermark triple with the §4.5 invariant 0 < L < C < H.
@st.composite
def base_watermarks_strategy(draw: st.DrawFn) -> Watermarks:
    low = draw(st.floats(min_value=5.0, max_value=40.0, allow_nan=False))
    commit = draw(st.floats(min_value=low + 5.0, max_value=low + 40.0, allow_nan=False))
    high = draw(st.floats(min_value=commit + 5.0, max_value=commit + 60.0, allow_nan=False))
    return Watermarks(low_s=low, high_s=high, commit_horizon_s=commit)


def _model_with_samples(samples: list[tuple[int, float]]) -> ReadingModel:
    model = ReadingModel()
    for words, dt in samples:
        model.observe(words_advanced=words, dt_ms=dt)
    return model


#: A run of >= 2 valid observations so the model leaves cold-start (dt floored 1ms).
warm_samples = st.lists(
    st.tuples(
        st.integers(min_value=1, max_value=500),
        st.floats(min_value=1.0, max_value=5000.0, allow_nan=False),
    ),
    min_size=2,
    max_size=20,
)


@given(base_watermarks_strategy(), warm_samples)
def test_adapt_preserves_ordering_invariant(
    base: Watermarks, samples: list[tuple[int, float]]
) -> None:
    """The §4.5 invariant 0 < L < C < H always holds after adaptation."""
    out = adapt_watermarks(base, _model_with_samples(samples))
    assert 0.0 < out.low_s < out.commit_horizon_s < out.high_s


@given(base_watermarks_strategy(), warm_samples)
def test_adapt_only_deepens_the_buffer(
    base: Watermarks, samples: list[tuple[int, float]]
) -> None:
    """Adaptation never makes a watermark *smaller* than its base (§4.6 safety)."""
    out = adapt_watermarks(base, _model_with_samples(samples))
    assert out.low_s >= base.low_s - 1e-6
    assert out.high_s >= base.high_s - 1e-6
    assert out.commit_horizon_s >= base.commit_horizon_s - 1e-6


@given(base_watermarks_strategy(), warm_samples)
def test_adapt_respects_bounded_growth(
    base: Watermarks, samples: list[tuple[int, float]]
) -> None:
    """No watermark exceeds ``max_multiple`` × its base (the growth ceiling).

    Tolerance is the 4-decimal rounding granularity (``round(·, 4)`` in
    ``adapt_watermarks`` can land a clamped value up to ~5e-5 above the exact
    ceiling — a benign rounding edge, the same class as MINOR-1 in DESIGN.md).
    ``commit`` and ``high`` may additionally be lifted to satisfy the ``L < C < H``
    /``min_band`` invariants when the *base* band is itself narrower than
    ``min_band_s``, so they are bounded by the larger of the multiple ceiling and
    that structural floor.
    """
    cfg = AdaptiveConfig()
    tol = 5e-5
    out = adapt_watermarks(base, _model_with_samples(samples), config=cfg)
    assert out.low_s <= base.low_s * cfg.max_multiple + tol
    # commit can be lifted to low+1 (ordering) even past its own multiple ceiling.
    commit_ceiling = max(base.commit_horizon_s * cfg.max_multiple, out.low_s + 1.0)
    assert out.commit_horizon_s <= commit_ceiling + tol
    # high may be bumped to low + min_band; bound it by that OR the multiple ceiling.
    high_ceiling = max(base.high_s * cfg.max_multiple, out.low_s + cfg.min_band_s)
    assert out.high_s <= high_ceiling + tol


@given(base_watermarks_strategy(), warm_samples)
def test_adapt_preserves_minimum_band(
    base: Watermarks, samples: list[tuple[int, float]]
) -> None:
    """The hysteresis band ``H − L`` is never below ``min_band_s`` (no thrash)."""
    cfg = AdaptiveConfig()
    out = adapt_watermarks(base, _model_with_samples(samples), config=cfg)
    assert out.high_s - out.low_s >= cfg.min_band_s - 1e-6


@given(base_watermarks_strategy())
def test_cold_start_returns_base_unchanged(base: Watermarks) -> None:
    """A <2-sample model leaves the base watermarks byte-for-byte (baseline parity)."""
    out = adapt_watermarks(base, ReadingModel())
    assert out.as_tuple() == base.as_tuple()


@given(base_watermarks_strategy(), warm_samples)
def test_adapt_is_deterministic(
    base: Watermarks, samples: list[tuple[int, float]]
) -> None:
    a = adapt_watermarks(base, _model_with_samples(samples))
    b = adapt_watermarks(base, _model_with_samples(samples))
    assert a.as_tuple() == b.as_tuple()


# --------------------------------------------------------------------------- #
# FairShareAllocator
# --------------------------------------------------------------------------- #

session_demands = st.builds(
    SessionDemand,
    session_id=st.text(min_size=1, max_size=6),
    deficit_s=st.floats(min_value=0.0, max_value=500.0, allow_nan=False),
    weight=st.floats(min_value=0.0, max_value=5.0, allow_nan=False),
    per_session_cap_s=st.one_of(
        st.none(), st.floats(min_value=0.0, max_value=500.0, allow_nan=False)
    ),
)


@st.composite
def demand_sets(draw: st.DrawFn) -> list[SessionDemand]:
    """A set of demands with unique session ids."""
    demands = draw(st.lists(session_demands, max_size=8))
    return [
        SessionDemand(
            session_id=f"sess_{i}",
            deficit_s=d.deficit_s,
            weight=d.weight,
            per_session_cap_s=d.per_session_cap_s,
        )
        for i, d in enumerate(demands)
    ]


pools = st.floats(min_value=0.0, max_value=2000.0, allow_nan=False)


@given(demand_sets(), pools)
def test_total_allocation_never_exceeds_pool(
    demands: list[SessionDemand], pool: float
) -> None:
    """THE spend invariant: the sum of caps never exceeds the shared pool (§12.2)."""
    alloc = FairShareAllocator().allocate(demands, pool_s=pool)
    assert alloc.total_s <= pool + 1e-5


@given(demand_sets(), pools)
def test_no_session_exceeds_its_ceiling(
    demands: list[SessionDemand], pool: float
) -> None:
    """No session is allocated more than its deficit (and its sub-cap, if set)."""
    alloc = FairShareAllocator().allocate(demands, pool_s=pool)
    for d in demands:
        cap = alloc.cap_for(d.session_id)
        assert cap <= d.deficit_s + 1e-5
        if d.per_session_cap_s is not None:
            assert cap <= d.per_session_cap_s + 1e-5


@given(demand_sets(), pools)
def test_non_needy_sessions_get_nothing(
    demands: list[SessionDemand], pool: float
) -> None:
    """A satisfied (deficit≈0) or zero-weight session is allocated nothing."""
    alloc = FairShareAllocator().allocate(demands, pool_s=pool)
    for d in demands:
        if d.deficit_s <= 1e-6 or d.weight <= 0.0:
            assert alloc.cap_for(d.session_id) == 0.0


@given(demand_sets())
def test_zero_pool_allocates_nothing(demands: list[SessionDemand]) -> None:
    """A closed/empty pool allocates zero to everyone (no spend)."""
    alloc = FairShareAllocator().allocate(demands, pool_s=0.0)
    assert alloc.total_s == 0.0
    for d in demands:
        assert alloc.cap_for(d.session_id) == 0.0


@given(demand_sets(), pools)
def test_all_caps_are_nonnegative(
    demands: list[SessionDemand], pool: float
) -> None:
    alloc = FairShareAllocator().allocate(demands, pool_s=pool)
    assert all(v >= 0.0 for v in alloc.caps.values())
    # Every input session has an entry in the allocation.
    assert {d.session_id for d in demands} <= set(alloc.caps)


@given(demand_sets(), pools)
def test_work_conserving_with_comparable_weights(
    demands: list[SessionDemand], pool: float
) -> None:
    """When demand exceeds the pool AND weights are comparable, ~the whole pool is used.

    A scarce pool should not be left idle while readers still need video. This holds
    cleanly when weights are within a sane band; see MINOR-2 in DESIGN.md for the
    *lopsided-weight* edge where a session with a near-zero weight (e.g. 1e-200) is
    dropped from water-filling once its proportional share rounds below epsilon,
    leaving its ceiling room unfilled — a genuine work-conservation gap the
    allocator's "work-conserving" docstring slightly overstates. We therefore scope
    this property to equal-ish weights, where the gap cannot arise.
    """
    needy = [d for d in demands if d.deficit_s > 1e-6 and d.weight >= 0.5]
    # All needy weights comparable (no >100x spread that would strand a tiny-weight one).
    assume(needy and all(0.5 <= d.weight <= 5.0 for d in needy))
    # Re-scope the demand set to only the comparable-weight needy sessions for the run.
    total_ceiling = sum(
        min(d.deficit_s, d.per_session_cap_s) if d.per_session_cap_s is not None else d.deficit_s
        for d in needy
    )
    assume(total_ceiling >= pool > 0.0)  # genuinely over-subscribed
    alloc = FairShareAllocator().allocate(needy, pool_s=pool)
    # Allocated close to the full pool (within a small water-filling tolerance).
    assert alloc.total_s >= min(pool, total_ceiling) - 1e-2


@given(demand_sets(), pools)
def test_allocation_is_deterministic(
    demands: list[SessionDemand], pool: float
) -> None:
    a = FairShareAllocator().allocate(demands, pool_s=pool)
    b = FairShareAllocator().allocate(demands, pool_s=pool)
    assert a.caps == b.caps


def test_lopsided_weight_strands_pool_MINOR2() -> None:
    """Regression pin for MINOR-2: a near-zero-weight needy session strands the pool.

    Two needy sessions, each deficit 1.0, pool 2.0 (exactly satisfiable). ``sess_1``
    has a vanishing weight, so after the 0.1 min-share floor it is dropped from
    water-filling (its proportional share rounds below epsilon) and never reaches
    its 1.0 deficit — leaving ~0.8 of the pool idle though a reader still needs it.
    This documents the work-conservation gap; ``xfail(strict)`` flips it the moment
    the allocator is taught to keep filling a stranded session.
    """
    import pytest

    demands = [
        SessionDemand(session_id="a", deficit_s=1.0, weight=1.0),
        SessionDemand(session_id="b", deficit_s=1.0, weight=1e-200),
    ]
    alloc = FairShareAllocator().allocate(demands, pool_s=2.0)
    # The documented invariants still hold (never over-spend, never over-ceiling):
    assert alloc.total_s <= 2.0 + 1e-9
    assert alloc.cap_for("a") <= 1.0 + 1e-9
    assert alloc.cap_for("b") <= 1.0 + 1e-9
    # ...but the pool is left under-filled — the gap.
    if alloc.total_s >= 2.0 - 1e-2:
        pytest.fail("work-conservation gap MINOR-2 appears fixed — un-pin this test")

