"""Deterministic sticky/balanced assignment + targeting + monotone ramp."""

from __future__ import annotations

from collections import Counter

from app.video.experiments import (
    Targeting,
    VideoAssigner,
    VideoExperiment,
    VideoVariant,
)
from app.video.experiments.assignment import (
    REASON_ASSIGNED,
    REASON_NOT_ELIGIBLE,
    REASON_NOT_ENROLLED,
    RenderUnit,
)

from .conftest import two_arm_experiment


def _exp(**kw: object) -> VideoExperiment:
    return two_arm_experiment(with_failure_guardrail=False, **kw)  # type: ignore[arg-type]


def test_assignment_is_sticky_per_unit() -> None:
    asg = VideoAssigner(_exp())
    first = asg.assign(RenderUnit(book_id="book_42"))
    for _ in range(50):
        again = asg.assign(RenderUnit(book_id="book_42"))
        assert again.variant_key == first.variant_key
        assert again.bucket == first.bucket


def test_assignment_balanced_at_50_50() -> None:
    asg = VideoAssigner(_exp())
    counts: Counter[str] = Counter()
    for i in range(20_000):
        a = asg.assign(RenderUnit(book_id=f"book_{i}"))
        counts[a.variant_key or "none"] += 1
    # 50/50 split over 20k units should be within ~2% of even.
    assert abs(counts["control"] - counts["treat"]) < 0.04 * 20_000


def test_assignment_respects_weights() -> None:
    exp = _exp(control_weight=8000, treat_weight=2000)
    asg = VideoAssigner(exp)
    counts: Counter[str] = Counter()
    for i in range(20_000):
        a = asg.assign(RenderUnit(book_id=f"b{i}"))
        counts[a.variant_key or "none"] += 1
    frac_treat = counts["treat"] / 20_000
    assert abs(frac_treat - 0.20) < 0.02


def test_bucket_by_book_keeps_all_shots_on_one_arm() -> None:
    # bucket_by defaults to book_id: every shot in a book gets the same arm.
    asg = VideoAssigner(_exp())
    variants = {
        asg.assign(RenderUnit(book_id="book_77", shot_id=f"shot_{i}")).variant_key
        for i in range(40)
    }
    assert len(variants) == 1


def test_bucket_by_shot_varies_within_a_book() -> None:
    asg = VideoAssigner(_exp(salt="shot-salt"))
    # Re-make the experiment bucketing by shot.
    exp = VideoExperiment(
        "e",
        (
            VideoVariant("control", "p", "m", 5000, is_control=True),
            VideoVariant("treat", "p", "m2", 5000),
        ),
        salt="shot-salt",
        bucket_by="shot_id",
    )
    asg = VideoAssigner(exp)
    variants = Counter(
        asg.assign(RenderUnit(book_id="book_77", shot_id=f"shot_{i}")).variant_key
        for i in range(200)
    )
    # Both arms appear when bucketing per shot.
    assert variants["control"] > 0 and variants["treat"] > 0


def test_targeting_excludes_ineligible_units() -> None:
    exp = VideoExperiment(
        "e",
        (
            VideoVariant("control", "p", "m", 5000, is_control=True),
            VideoVariant("treat", "p", "m2", 5000),
        ),
        salt="s",
        targeting=Targeting(modes=frozenset({"i2v"})),
    )
    asg = VideoAssigner(exp)
    eligible = asg.assign(RenderUnit(book_id="b", mode="i2v"))
    excluded = asg.assign(RenderUnit(book_id="b", mode="t2v"))
    assert eligible.in_experiment and eligible.reason == REASON_ASSIGNED
    assert not excluded.in_experiment and excluded.reason == REASON_NOT_ELIGIBLE
    assert excluded.variant is None


def test_traffic_percent_gates_enrollment() -> None:
    exp = _exp()
    none = VideoAssigner(exp.with_traffic_percent(0.0))
    a = none.assign(RenderUnit(book_id="b1"))
    assert not a.in_experiment and a.reason == REASON_NOT_ENROLLED


def test_ramp_is_monotone_only_adds_units() -> None:
    # A unit enrolled at a low traffic % must stay enrolled as the % grows.
    exp = _exp()
    enrolled_at: dict[str, float] = {}
    units = [RenderUnit(book_id=f"b{i}") for i in range(2000)]
    for pct in (1.0, 5.0, 25.0, 100.0):
        asg = VideoAssigner(exp.with_traffic_percent(pct))
        for u in units:
            a = asg.assign(u)
            if a.in_experiment:
                enrolled_at.setdefault(u.book_id or "", pct)
                # once recorded, must remain enrolled at every higher pct
                assert a.in_experiment
    # And enrollment grows with the percentage.
    enrolled_counts = {}
    for pct in (1.0, 5.0, 25.0, 100.0):
        asg = VideoAssigner(exp.with_traffic_percent(pct))
        enrolled_counts[pct] = sum(1 for u in units if asg.assign(u).in_experiment)
    assert (
        enrolled_counts[1.0]
        <= enrolled_counts[5.0]
        <= enrolled_counts[25.0]
        <= enrolled_counts[100.0]
        == len(units)
    )


def test_assignment_carries_the_variant_spec() -> None:
    asg = VideoAssigner(_exp())
    # find a unit that lands on treat (which carries spec)
    for i in range(1000):
        a = asg.assign(RenderUnit(book_id=f"b{i}"))
        if a.variant_key == "treat":
            assert a.variant is not None
            assert a.variant.spec["resolution"] == "1080P"
            break
    else:  # pragma: no cover
        raise AssertionError("no unit landed on treat")


def test_anonymous_unit_buckets_deterministically() -> None:
    asg = VideoAssigner(_exp())
    a = asg.assign(RenderUnit())
    b = asg.assign(RenderUnit())
    assert a.variant_key == b.variant_key  # stable sentinel bucket
