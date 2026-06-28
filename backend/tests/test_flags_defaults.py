"""Canonical Kinora flag/experiment definitions — they must be valid + sane."""

from __future__ import annotations

from app.flags.context import EvalContext
from app.flags.defaults import (
    LIVE_VIDEO,
    RENDER_LADDER,
    WATERMARK_BANDS,
    default_experiments,
    default_flags,
    render_ladder_flag,
)
from app.flags.evaluator import FlagEvaluator
from app.flags.models import FlagSnapshot
from app.flags.serialization import experiment_to_dict, flag_to_dict


def test_all_default_flags_are_valid_and_serializable() -> None:
    flags = default_flags()
    assert len(flags) >= 5
    snap = FlagSnapshot.from_flags(flags)
    for flag in flags:
        flag_to_dict(flag)  # serializes without raising
    assert LIVE_VIDEO in snap.flags


def test_live_video_defaults_off() -> None:
    flags = {f.key: f for f in default_flags()}
    live = flags[LIVE_VIDEO]
    assert live.enabled is False
    ev = FlagEvaluator(FlagSnapshot.from_flags((live,)))
    assert ev.evaluate(LIVE_VIDEO, EvalContext.of("u")).value is False


def test_render_ladder_fast_skimmer_rule() -> None:
    ladder = render_ladder_flag()
    ev = FlagEvaluator(FlagSnapshot.from_flags((ladder,)))
    # a fast skimmer is forced to the Ken-Burns lane
    fast = ev.evaluate(RENDER_LADDER, EvalContext.of("u", velocity_wps=12))
    assert fast.value == "kenburns"
    # a normal reader falls through to full video
    slow = ev.evaluate(RENDER_LADDER, EvalContext.of("u", velocity_wps=3))
    assert slow.value == "full"


def test_all_default_experiments_are_valid() -> None:
    exps = default_experiments()
    assert len(exps) >= 2
    for exp in exps:
        experiment_to_dict(exp)  # serializes without raising
        assert exp.control is not None
        assert exp.primary_metric is not None


def test_watermark_bands_are_well_formed() -> None:
    for band in WATERMARK_BANDS.values():
        assert band["low_s"] < band["commit_s"] < band["high_s"]
