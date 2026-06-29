"""Property + metamorphic tests for the §9.5 Critic routing (``decide_qa``).

``decide_qa`` is the gate that decides whether a clip ships and, if not, *which*
repair it gets. Its correctness is safety-critical: a loosened gate ships slop, a
mis-routed repair wastes budget. These tests pin the four-way pass gate exactly at
its thresholds and check the routing's documented branch order, plus three
metamorphic relations (monotonicity, advisory-neutrality, score bounds).
"""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from app.agents.contracts import RepairAction, Verdict
from app.agents.critic import DEFAULT_THRESHOLDS, QAThresholds, decide_qa
from app.render.reward import RewardAdvice
from app.verification.properties.relations import degrade_qa, improve_qa
from app.verification.properties.strategies import (
    CCS_MIN,
    MOTION_ARTIFACT_MAX,
    STYLE_DRIFT_MAX,
    failing_qa_scores,
    passing_qa_scores,
    qa_scores,
)

T = DEFAULT_THRESHOLDS


def _passes(ccs: float, drift: float, tl: bool, motion: float, t: QAThresholds = T) -> bool:
    return (
        ccs >= t.ccs_min
        and drift <= t.style_drift_max
        and tl
        and motion <= t.motion_artifact_max
    )


@given(qa_scores())
def test_verdict_matches_the_four_way_gate_exactly(
    scores: tuple[float, float, bool, float],
) -> None:
    """PASS iff all four checks hold — the gate is a literal conjunction (§9.5)."""
    ccs, drift, tl, motion = scores
    verdict, action, _ = decide_qa(ccs, drift, tl, motion)
    if _passes(ccs, drift, tl, motion):
        assert verdict is Verdict.PASS
        assert action is RepairAction.ACCEPT
    else:
        assert verdict is Verdict.FAIL
        assert action is not RepairAction.ACCEPT


@given(qa_scores())
def test_score_is_bounded_and_deterministic(
    scores: tuple[float, float, bool, float],
) -> None:
    """The composite score is in [0,1] and stable across calls."""
    ccs, drift, tl, motion = scores
    _, _, s1 = decide_qa(ccs, drift, tl, motion)
    _, _, s2 = decide_qa(ccs, drift, tl, motion)
    assert 0.0 <= s1 <= 1.0
    assert s1 == s2


@given(passing_qa_scores())
def test_pass_is_monotone_under_improvement(
    scores: tuple[float, float, bool, float],
) -> None:
    """Metamorphic: improving every axis can't turn a PASS into a FAIL (§9.5).

    Starts from an already-passing scorecard (sampled directly, not filtered) and
    strictly improves the numeric axes; the verdict must stay PASS/ACCEPT.
    """
    ccs, drift, tl, motion = scores
    assert decide_qa(ccs, drift, tl, motion)[0] is Verdict.PASS
    ccs2, drift2, motion2 = improve_qa(ccs, drift, motion)
    better_verdict, action, _ = decide_qa(ccs2, drift2, tl, motion2)
    assert better_verdict is Verdict.PASS
    assert action is RepairAction.ACCEPT


@given(failing_qa_scores())
def test_fail_is_monotone_under_degradation(
    scores: tuple[float, float, bool, float],
) -> None:
    """Metamorphic dual: worsening every axis can't turn a FAIL into a PASS."""
    ccs, drift, tl, motion = scores
    assert decide_qa(ccs, drift, tl, motion)[0] is Verdict.FAIL
    ccs2, drift2, motion2 = degrade_qa(ccs, drift, motion)
    worse_verdict, _, _ = decide_qa(ccs2, drift2, tl, motion2)
    assert worse_verdict is Verdict.FAIL


@given(qa_scores())
def test_score_is_monotone_in_each_axis(
    scores: tuple[float, float, bool, float],
) -> None:
    """The composite score never *drops* when every axis improves."""
    ccs, drift, tl, motion = scores
    _, _, base = decide_qa(ccs, drift, tl, motion)
    ccs2, drift2, motion2 = improve_qa(ccs, drift, motion)
    _, _, better = decide_qa(ccs2, drift2, tl, motion2)
    assert better >= base - 1e-9


@given(qa_scores(), st.booleans())
def test_timeline_failure_routes_to_conflict_or_evolve(
    scores: tuple[float, float, bool, float], supported: bool
) -> None:
    """A timeline contradiction routes to §7.2 (evolve iff text-supported)."""
    ccs, drift, _, motion = scores
    # Force a timeline failure but keep retries available so the cap doesn't win.
    verdict, action, _ = decide_qa(
        ccs, drift, False, motion, textual_evolution_supported=supported, retries_exhausted=False
    )
    assert verdict is Verdict.FAIL
    expected = RepairAction.EVOLVE_CANON if supported else RepairAction.RAISE_CONFLICT
    assert action is expected


@given(
    st.floats(min_value=0.0, max_value=CCS_MIN - 1e-3, allow_nan=False),
    st.floats(min_value=0.0, max_value=STYLE_DRIFT_MAX, allow_nan=False),
    st.floats(min_value=0.0, max_value=MOTION_ARTIFACT_MAX, allow_nan=False),
)
def test_identity_drift_with_ok_style_tightens_refs(
    ccs: float, drift: float, motion: float
) -> None:
    """CCS fail with style OK (and timeline/motion OK) → tighten references (§9.5)."""
    verdict, action, _ = decide_qa(ccs, drift, True, motion)
    assert verdict is Verdict.FAIL
    assert action is RepairAction.REGEN_TIGHTEN_REFS


@given(
    st.floats(min_value=CCS_MIN, max_value=1.0, allow_nan=False),
    st.floats(min_value=STYLE_DRIFT_MAX + 1e-3, max_value=1.0, allow_nan=False),
    st.floats(min_value=0.0, max_value=MOTION_ARTIFACT_MAX, allow_nan=False),
)
def test_style_failure_reprompts_style(ccs: float, drift: float, motion: float) -> None:
    """A style-drift failure (CCS/timeline/motion OK) routes to the style re-prompt.

    Style is checked *after* identity in the routing, so to isolate the style
    branch CCS must be OK; with timeline+motion OK the only failing axis is style.
    """
    verdict, action, _ = decide_qa(ccs, drift, True, motion)
    assert verdict is Verdict.FAIL
    assert action is RepairAction.REPROMPT_STYLE


@given(
    st.floats(min_value=CCS_MIN, max_value=1.0, allow_nan=False),
    st.floats(min_value=0.0, max_value=STYLE_DRIFT_MAX, allow_nan=False),
    st.floats(min_value=MOTION_ARTIFACT_MAX + 1e-3, max_value=1.0, allow_nan=False),
)
def test_motion_only_failure_is_a_new_seed(
    ccs: float, drift: float, motion: float
) -> None:
    """CCS/style/timeline OK but motion artifact over max → regen with a new seed."""
    verdict, action, _ = decide_qa(ccs, drift, True, motion)
    assert verdict is Verdict.FAIL
    assert action is RepairAction.REGEN_NEW_SEED


@given(failing_qa_scores())
def test_retries_exhausted_always_degrades_a_failure(
    scores: tuple[float, float, bool, float],
) -> None:
    """The §9.5 retry cap: an exhausted *failing* clip degrades, whatever failed."""
    ccs, drift, tl, motion = scores
    verdict, action, _ = decide_qa(ccs, drift, tl, motion, retries_exhausted=True)
    assert verdict is Verdict.FAIL
    assert action is RepairAction.DEGRADE


@given(passing_qa_scores())
def test_exhaustion_never_overrides_a_pass(
    scores: tuple[float, float, bool, float],
) -> None:
    """A passing clip accepts even at the retry cap (the cap only ends failures)."""
    ccs, drift, tl, motion = scores
    verdict, action, _ = decide_qa(ccs, drift, tl, motion, retries_exhausted=True)
    assert verdict is Verdict.PASS
    assert action is RepairAction.ACCEPT


@given(qa_scores(), st.floats(min_value=0.0, max_value=1.0), st.booleans())
def test_advice_is_strictly_advisory(
    scores: tuple[float, float, bool, float], reward: float, flagged: bool
) -> None:
    """Metamorphic: passing a RewardAdvice never changes verdict/action/score (§9.5/§13).

    The learned-reward layer is advisory only — it can neither rescue a failed clip
    nor block a passing one. So ``decide_qa`` with any advice must be byte-identical
    to ``decide_qa`` with ``advice=None`` (the cold-start default).
    """
    ccs, drift, tl, motion = scores
    advice = RewardAdvice(
        reward=reward, anomaly_score=reward, flagged_for_review=flagged
    )
    base = decide_qa(ccs, drift, tl, motion)
    with_advice = decide_qa(ccs, drift, tl, motion, advice=advice)
    assert with_advice == base


@given(qa_scores(), st.floats(min_value=0.5, max_value=0.99), st.floats(0.0, 0.07))
def test_calibration_never_loosens_the_floor(
    scores: tuple[float, float, bool, float], cal_ccs: float, cal_drift: float
) -> None:
    """A *tighter* threshold set can only turn passes into fails, never the reverse.

    This guards the §9.5/§13 promise that calibrated thresholds are "never looser
    than the pre-registered floor": with stricter thresholds, every clip the strict
    gate passes the default gate also passes.
    """
    ccs, drift, tl, motion = scores
    strict = QAThresholds(
        ccs_min=max(T.ccs_min, cal_ccs),
        style_drift_max=min(T.style_drift_max, cal_drift),
        motion_artifact_max=T.motion_artifact_max,
    )
    strict_verdict, _, _ = decide_qa(ccs, drift, tl, motion, thresholds=strict)
    default_verdict, _, _ = decide_qa(ccs, drift, tl, motion)
    if strict_verdict is Verdict.PASS:
        assert default_verdict is Verdict.PASS
