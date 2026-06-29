"""Property + metamorphic tests for the §4.3/§4.4/§4.6 scheduler zone math.

The scheduler's whole self-tuning story rests on a few lines of pure arithmetic:
ETA = gap / velocity, a three-way zone classification, a velocity clamp, and a
promotion-suspension gate. These properties verify the arithmetic's invariants
(clamp idempotence, ETA sign, the zone partition) and — the load-bearing part —
the two metamorphic relations §4.6 leans on:

* **velocity monotonicity** — a faster reader pulls a shot *toward* committed,
* **translation invariance** — only the focus→shot *gap* matters, not absolute
  position.
"""

from __future__ import annotations

from hypothesis import assume, given
from hypothesis import strategies as st

from app.scheduler.zones import (
    VELOCITY_CLAMP_HIGH,
    VELOCITY_CLAMP_LOW,
    Zone,
    clamp_velocity,
    classify,
    eta_seconds,
    trajectory_is_stable,
    viewer_zone,
)
from app.verification.properties.relations import scale_gap, shift_positions
from app.verification.properties.strategies import (
    FakeStability,
    focus_words,
    horizons,
    positive_velocities,
    raw_velocities,
    stability_states,
    word_index_starts,
)

# --------------------------------------------------------------------------- #
# clamp_velocity
# --------------------------------------------------------------------------- #


@given(raw_velocities)
def test_clamp_lands_in_band(v: float) -> None:
    assert VELOCITY_CLAMP_LOW <= clamp_velocity(v) <= VELOCITY_CLAMP_HIGH


@given(raw_velocities)
def test_clamp_is_idempotent(v: float) -> None:
    once = clamp_velocity(v)
    assert clamp_velocity(once) == once


@given(raw_velocities)
def test_clamp_ignores_sign(v: float) -> None:
    """Clamp uses magnitude — a backward reader clamps like its forward twin."""
    assert clamp_velocity(v) == clamp_velocity(-v)


@given(raw_velocities, raw_velocities)
def test_clamp_is_monotone_in_magnitude(a: float, b: float) -> None:
    if abs(a) <= abs(b):
        assert clamp_velocity(a) <= clamp_velocity(b)


# --------------------------------------------------------------------------- #
# eta_seconds
# --------------------------------------------------------------------------- #


@given(focus_words, word_index_starts, positive_velocities)
def test_eta_sign_tracks_gap(start: int, focus: int, v: float) -> None:
    """ETA is positive iff the shot is ahead, zero iff at, negative iff behind."""
    eta = eta_seconds(start, focus, v)
    if start > focus:
        assert eta > 0
    elif start == focus:
        assert eta == 0
    else:
        assert eta < 0


@given(focus_words, word_index_starts)
def test_eta_never_divides_by_zero(start: int, focus: int) -> None:
    """Even velocity 0 yields a finite ETA (the 0.1 wps floor guards the divide)."""
    eta = eta_seconds(start, focus, 0.0)
    assert eta == eta  # not NaN
    assert abs(eta) != float("inf")


@given(focus_words, word_index_starts, positive_velocities, st.integers(-10_000, 10_000))
def test_eta_is_translation_invariant(
    start: int, focus: int, v: float, delta: int
) -> None:
    """Metamorphic (§4.3): shifting focus and start together leaves ETA unchanged."""
    base = eta_seconds(start, focus, v)
    f2, s2 = shift_positions(focus, start, delta)
    shifted = eta_seconds(s2, f2, v)
    assert abs(base - shifted) < 1e-6


@given(focus_words, word_index_starts, positive_velocities, st.floats(0.5, 4.0))
def test_eta_scales_inversely_with_velocity(
    start: int, focus: int, v: float, k: float
) -> None:
    """Metamorphic (§4.6): a ``k``× faster reader gets a ``1/k``× ETA.

    Verified via the gap: scaling the gap by ``k`` is equivalent to dividing
    velocity by ``k``, so ``eta(k·gap, v) == k · eta(gap, v)``.
    """
    assume(v >= 0.1)  # below the floor, the clamp dominates and the relation bends
    base = eta_seconds(start, focus, v)
    f2, s2 = scale_gap(focus, start, k)
    scaled = eta_seconds(s2, f2, v)
    # round() in scale_gap adds <1 word of slack; allow a velocity-scaled epsilon.
    assert abs(scaled - k * base) <= 1.0 / v + 1e-6


# --------------------------------------------------------------------------- #
# classify — the three-zone partition
# --------------------------------------------------------------------------- #


@given(st.floats(-1e6, 1e6, allow_nan=False), horizons())
def test_zone_partition_is_total_and_ordered(
    eta: float, hz: tuple[float, float]
) -> None:
    """Every ETA lands in exactly one zone, ordered committed<spec<cold by ETA."""
    commit, spec = hz
    zone = classify(eta, commit_horizon_s=commit, spec_horizon_s=spec)
    assert zone in (Zone.COMMITTED, Zone.SPECULATIVE, Zone.COLD)
    if eta < commit:
        assert zone is Zone.COMMITTED
    elif eta <= spec:
        assert zone is Zone.SPECULATIVE
    else:
        assert zone is Zone.COLD


@given(st.floats(-1e6, 1e6, allow_nan=False), st.floats(-1e6, 1e6, allow_nan=False), horizons())
def test_zone_is_monotone_in_eta(a: float, b: float, hz: tuple[float, float]) -> None:
    """A larger ETA never maps to a *nearer* zone (committed<spec<cold)."""
    commit, spec = hz
    rank = {Zone.COMMITTED: 0, Zone.SPECULATIVE: 1, Zone.COLD: 2}
    za = classify(min(a, b), commit_horizon_s=commit, spec_horizon_s=spec)
    zb = classify(max(a, b), commit_horizon_s=commit, spec_horizon_s=spec)
    assert rank[za] <= rank[zb]


@given(focus_words, word_index_starts, positive_velocities, horizons(), st.floats(1.01, 4.0))
def test_faster_reader_pulls_shot_toward_committed(
    start: int, focus: int, v: float, hz: tuple[float, float], k: float
) -> None:
    """Metamorphic (§4.6): raising velocity never pushes a shot to a *farther* zone."""
    assume(start >= focus)  # a forward shot (the promotion path)
    commit, spec = hz
    rank = {Zone.COMMITTED: 0, Zone.SPECULATIVE: 1, Zone.COLD: 2}
    slow = classify(
        eta_seconds(start, focus, v), commit_horizon_s=commit, spec_horizon_s=spec
    )
    fast = classify(
        eta_seconds(start, focus, v * k), commit_horizon_s=commit, spec_horizon_s=spec
    )
    assert rank[fast] <= rank[slow]


# --------------------------------------------------------------------------- #
# viewer_zone — promotion suspension under skim / budget pressure
# --------------------------------------------------------------------------- #


@given(st.floats(-1e6, 1e6, allow_nan=False), st.booleans(), st.booleans(), horizons())
def test_viewer_zone_never_promotes_above_classification(
    eta: float, stable: bool, budget_ok: bool, hz: tuple[float, float]
) -> None:
    """The badge zone is never *nearer* than the raw ETA classification (§5.3).

    viewer_zone may demote committed→speculative under skim/budget pressure, but it
    must never claim a shot is more-committed than its ETA says.
    """
    commit, spec = hz
    rank = {Zone.COMMITTED: 0, Zone.SPECULATIVE: 1, Zone.COLD: 2}
    raw = classify(eta, commit_horizon_s=commit, spec_horizon_s=spec)
    seen = viewer_zone(
        eta, stable=stable, budget_ok=budget_ok, commit_horizon_s=commit, spec_horizon_s=spec
    )
    assert rank[seen] >= rank[raw]


@given(st.floats(-1e6, 1e6, allow_nan=False), horizons())
def test_committed_demotes_to_speculative_when_unstable_or_broke(
    eta: float, hz: tuple[float, float]
) -> None:
    """A near shot rides the keyframe ladder when not (stable AND budget_ok) (§4.6)."""
    commit, spec = hz
    assume(eta < commit)  # would classify COMMITTED
    for stable, budget_ok in [(False, True), (True, False), (False, False)]:
        seen = viewer_zone(
            eta, stable=stable, budget_ok=budget_ok, commit_horizon_s=commit, spec_horizon_s=spec
        )
        assert seen is Zone.SPECULATIVE
    # Only the stable+solvent reader sees full video.
    assert (
        viewer_zone(eta, stable=True, budget_ok=True, commit_horizon_s=commit, spec_horizon_s=spec)
        is Zone.COMMITTED
    )


def test_viewer_zone_none_eta_is_cold() -> None:
    assert (
        viewer_zone(None, stable=True, budget_ok=True, commit_horizon_s=45, spec_horizon_s=240)
        is Zone.COLD
    )


# --------------------------------------------------------------------------- #
# trajectory_is_stable — skim detection
# --------------------------------------------------------------------------- #


@given(stability_states)
def test_stability_implies_within_clamp_and_not_oscillating(
    state: FakeStability,
) -> None:
    """Stable ⇒ raw velocity within the clamp ceiling AND not oscillating (§4.6)."""
    if trajectory_is_stable(state):
        assert abs(state.raw_velocity_wps) <= VELOCITY_CLAMP_HIGH
        assert not state.oscillating


@given(st.floats(0.0, VELOCITY_CLAMP_HIGH, allow_nan=False))
def test_within_clamp_and_steady_is_stable(v: float) -> None:
    """In-band velocity with no oscillation is stable."""
    assert trajectory_is_stable(FakeStability(raw_velocity_wps=v, oscillating=False))


@given(st.floats(VELOCITY_CLAMP_HIGH + 1e-3, 1e4, allow_nan=False), st.booleans())
def test_over_ceiling_is_always_unstable(v: float, osc: bool) -> None:
    """A skim above the clamp ceiling is unstable regardless of oscillation."""
    assert not trajectory_is_stable(FakeStability(raw_velocity_wps=v, oscillating=osc))


@given(raw_velocities)
def test_oscillation_forces_instability(v: float) -> None:
    assert not trajectory_is_stable(FakeStability(raw_velocity_wps=v, oscillating=True))
