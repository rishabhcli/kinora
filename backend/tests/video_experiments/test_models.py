"""Definition-layer validation: variants, metrics, targeting, experiment."""

from __future__ import annotations

import pytest

from app.video.experiments import (
    ACCEPT_RATE,
    FAILURE_RATE,
    MetricDirection,
    MetricKind,
    Targeting,
    VideoExperiment,
    VideoExperimentError,
    VideoMetric,
    VideoVariant,
    expected_allocation,
)


def _variant(key: str, weight: int, *, control: bool = False) -> VideoVariant:
    return VideoVariant(key, "dashscope", "model-" + key, weight, is_control=control)


def test_variant_requires_provider_and_model() -> None:
    with pytest.raises(VideoExperimentError):
        VideoVariant("v", "", "m", 5000)
    with pytest.raises(VideoExperimentError):
        VideoVariant("v", "p", "", 5000)
    with pytest.raises(VideoExperimentError):
        VideoVariant("", "p", "m", 5000)


def test_variant_spec_is_frozen_copy() -> None:
    src = {"resolution": "1080P"}
    v = VideoVariant("v", "p", "m", 5000, spec=src)
    src["resolution"] = "720P"  # mutate the original
    assert v.spec["resolution"] == "1080P"  # variant unaffected
    assert v.spec_dict() == {"resolution": "1080P"}
    with pytest.raises(TypeError):  # read-only mapping
        v.spec["x"] = 1  # type: ignore[index]


def test_metric_validation() -> None:
    with pytest.raises(VideoExperimentError):
        VideoMetric("")
    with pytest.raises(VideoExperimentError):
        VideoMetric("k", guardrail_margin=-0.1)


def test_experiment_requires_two_variants() -> None:
    with pytest.raises(VideoExperimentError):
        VideoExperiment("e", (_variant("a", 10_000, control=True),), salt="s")


def test_experiment_weights_must_sum_to_basis_points() -> None:
    with pytest.raises(VideoExperimentError):
        VideoExperiment(
            "e",
            (_variant("a", 4000, control=True), _variant("b", 5000)),
            salt="s",
        )


def test_experiment_exactly_one_control() -> None:
    with pytest.raises(VideoExperimentError):  # zero controls
        VideoExperiment("e", (_variant("a", 5000), _variant("b", 5000)), salt="s")
    with pytest.raises(VideoExperimentError):  # two controls
        VideoExperiment(
            "e",
            (_variant("a", 5000, control=True), _variant("b", 5000, control=True)),
            salt="s",
        )


def test_experiment_rejects_duplicate_variant_keys() -> None:
    with pytest.raises(VideoExperimentError):
        VideoExperiment(
            "e",
            (_variant("a", 5000, control=True), VideoVariant("a", "p", "m2", 5000)),
            salt="s",
        )


def test_experiment_requires_single_primary_metric() -> None:
    # two non-guardrail metrics is ambiguous
    with pytest.raises(VideoExperimentError):
        VideoExperiment(
            "e",
            (_variant("a", 5000, control=True), _variant("b", 5000)),
            salt="s",
            metrics=(VideoMetric("m1"), VideoMetric("m2")),
        )


def test_experiment_rejects_duplicate_metric_keys() -> None:
    with pytest.raises(VideoExperimentError):
        VideoExperiment(
            "e",
            (_variant("a", 5000, control=True), _variant("b", 5000)),
            salt="s",
            metrics=(VideoMetric("m1"), VideoMetric("m1", is_guardrail=True)),
        )


def test_experiment_bounds_traffic_and_samples_and_duration() -> None:
    base = {
        "key": "e",
        "variants": (_variant("a", 5000, control=True), _variant("b", 5000)),
        "salt": "s",
    }
    with pytest.raises(VideoExperimentError):
        VideoExperiment(**base, traffic_percent=101.0)  # type: ignore[arg-type]
    with pytest.raises(VideoExperimentError):
        VideoExperiment(**base, min_samples_per_arm=0)  # type: ignore[arg-type]
    with pytest.raises(VideoExperimentError):
        VideoExperiment(**base, max_duration_s=0.0)  # type: ignore[arg-type]


def test_experiment_accessors() -> None:
    exp = VideoExperiment(
        "e",
        (_variant("control", 7000, control=True), _variant("t1", 2000), _variant("t2", 1000)),
        salt="s",
        metrics=(
            VideoMetric(ACCEPT_RATE, MetricKind.PROPORTION, MetricDirection.INCREASE),
            VideoMetric(FAILURE_RATE, is_guardrail=True),
        ),
    )
    assert exp.control.key == "control"
    assert {v.key for v in exp.treatments} == {"t1", "t2"}
    assert exp.primary_metric is not None and exp.primary_metric.key == ACCEPT_RATE
    assert [m.key for m in exp.guardrails] == [FAILURE_RATE]
    assert exp.variant("t2").describe() == "dashscope/model-t2"
    with pytest.raises(VideoExperimentError):
        exp.variant("nope")


def test_expected_allocation_fractions() -> None:
    exp = VideoExperiment(
        "e",
        (_variant("control", 7000, control=True), _variant("t", 3000)),
        salt="s",
    )
    alloc = expected_allocation(exp)
    assert alloc == {"control": 0.7, "t": 0.3}


def test_with_traffic_percent_returns_validated_copy() -> None:
    exp = VideoExperiment(
        "e",
        (_variant("control", 5000, control=True), _variant("t", 5000)),
        salt="s",
        traffic_percent=100.0,
    )
    ramped = exp.with_traffic_percent(5.0)
    assert ramped.traffic_percent == 5.0
    assert exp.traffic_percent == 100.0  # original unchanged
    assert ramped.key == exp.key


# --- Targeting -------------------------------------------------------------- #


def test_targeting_empty_matches_all() -> None:
    t = Targeting()
    assert t.matches(mode="t2v", book_id="b", resolution="720P", duration_s=5.0)
    assert t.matches()


def test_targeting_mode_and_resolution_filters() -> None:
    t = Targeting(modes=frozenset({"i2v"}), resolutions=frozenset({"1080P"}))
    assert t.matches(mode="i2v", resolution="1080P")
    assert not t.matches(mode="t2v", resolution="1080P")
    assert not t.matches(mode="i2v", resolution="720P")
    assert not t.matches(mode=None, resolution="1080P")  # missing attr fails the filter


def test_targeting_duration_bounds() -> None:
    t = Targeting(min_duration_s=4.0, max_duration_s=8.0)
    assert t.matches(duration_s=5.0)
    assert not t.matches(duration_s=3.0)
    assert not t.matches(duration_s=9.0)
    assert not t.matches(duration_s=None)


def test_targeting_book_cohort() -> None:
    t = Targeting(book_ids=frozenset({"book_pilot"}))
    assert t.matches(book_id="book_pilot")
    assert not t.matches(book_id="book_other")
