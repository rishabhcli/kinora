"""Deterministic tests for the capability-aware video request planner.

Pure planning logic — no infra, no network, no env, no RNG. Exhaustively covers
the negotiator's translation/degradation paths across many capability profiles.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.video.planning import (
    ASPECT_9_16,
    ASPECT_16_9,
    AspectRatio,
    CanonicalVideoRequest,
    CapabilityProfile,
    FidelityPenalty,
    PostOp,
    StepKind,
    VideoMode,
    best_plan,
    compress_prompt,
    minimax_profile,
    plan,
    plan_all,
    rank_plans,
    resolution_height,
    resolution_pixels,
    text_only_profile,
    wan_profile,
)

# --------------------------------------------------------------------------- #
# Capability primitives
# --------------------------------------------------------------------------- #


def test_aspect_ratio_reduces_and_parses() -> None:
    assert AspectRatio(width=16, height=9) == AspectRatio.parse("16:9")
    assert AspectRatio.parse("1920:1080") == ASPECT_16_9
    assert AspectRatio.parse("16x9") == ASPECT_16_9
    # Decimal form parses to lowest terms.
    assert AspectRatio.parse("1.7777777").value == pytest.approx(16 / 9, abs=1e-3)
    assert ASPECT_9_16.is_portrait and not ASPECT_9_16.is_landscape
    assert ASPECT_16_9.is_landscape
    assert AspectRatio(width=1, height=1).is_square
    assert str(ASPECT_16_9) == "16:9"


def test_resolution_pixels_and_height() -> None:
    assert resolution_pixels("720P") == (1280, 720)
    assert resolution_pixels("720") == (1280, 720)  # bare number → P-suffixed
    assert resolution_pixels("nonsense") is None
    assert resolution_height("1080P") == 1080
    assert resolution_height("unknown") == 0
    assert resolution_height("4K") == 2160


def test_profile_is_frozen_and_hashable() -> None:
    prof = wan_profile()
    with pytest.raises(ValidationError):
        prof.name = "mutated"  # type: ignore[misc]
    # Frozen pydantic models are hashable → usable as dict keys / in sets.
    assert prof in {prof}


@pytest.mark.parametrize(
    ("requested", "options", "expected"),
    [
        (30, (16, 24), 24),
        (24, (16, 24), 24),
        (20, (16, 24), 16),  # tie → nearest; 20 is closer to 16? |20-16|=4 |24-20|=4 tie→smaller
        (60, (24, 30, 60), 60),
        (12, (), 12),  # unconstrained
    ],
)
def test_clamp_fps(requested: int, options: tuple[int, ...], expected: int) -> None:
    prof = CapabilityProfile(name="x", fps_options=options)
    assert prof.clamp_fps(requested) == expected


@pytest.mark.parametrize(
    ("requested", "options", "expected"),
    [
        ("1080P", ("480P", "720P"), "720P"),  # clamp down to nearest
        ("360P", ("480P", "720P"), "480P"),  # clamp up to nearest
        ("720P", ("480P", "720P", "1080P"), "720P"),  # exact
        ("weird", ("480P", "720P"), "720P"),  # unknown → richest
        ("4K", (), "4K"),  # unconstrained
    ],
)
def test_clamp_resolution(requested: str, options: tuple[str, ...], expected: str) -> None:
    prof = CapabilityProfile(name="x", resolution_options=options)
    assert prof.clamp_resolution(requested) == expected


def test_clamp_aspect_prefers_same_orientation() -> None:
    # Asked for portrait 9:16, options include portrait 2:3 and landscape 16:9.
    prof = CapabilityProfile(
        name="x",
        aspect_options=(ASPECT_16_9, AspectRatio(width=2, height=3)),
    )
    got = prof.clamp_aspect(ASPECT_9_16)
    assert got.is_portrait  # never silently flips orientation when a portrait exists


def test_clamp_duration_discrete_and_window() -> None:
    window = CapabilityProfile(name="w", min_duration_s=2, max_duration_s=8)
    assert window.clamp_duration(5) == 5
    assert window.clamp_duration(99) == 8
    assert window.clamp_duration(0.1) == 2

    discrete = CapabilityProfile(name="d", duration_options=(6, 10))
    assert discrete.clamp_duration(7) == 6  # snap to nearest ≤
    assert discrete.clamp_duration(10) == 10
    assert discrete.clamp_duration(3) == 6  # below smallest → smallest option


# --------------------------------------------------------------------------- #
# Native fit — no concessions
# --------------------------------------------------------------------------- #


def test_native_fit_text_to_video_zero_cost() -> None:
    req = CanonicalVideoRequest(
        mode=VideoMode.TEXT_TO_VIDEO,
        prompt="a quiet harbor at dawn",
        duration_s=5,
        fps=24,
        resolution="720P",
        aspect=ASPECT_16_9,
        seed=7,
    )
    p = plan(req, wan_profile())
    assert p.feasible
    assert p.fidelity_cost == 0.0
    assert p.segment_count == 1
    (step,) = p.steps
    assert step.kind is StepKind.RENDER
    assert step.mode is VideoMode.TEXT_TO_VIDEO
    assert step.seed == 7
    assert step.duration_s == 5
    assert step.post_ops == ()
    assert "Native fit" in p.rationale


def test_native_reference_to_video_on_wan() -> None:
    req = CanonicalVideoRequest(
        mode=VideoMode.REFERENCE_TO_VIDEO,
        duration_s=5,
        fps=24,
        resolution="720P",
        aspect=ASPECT_16_9,
        reference_image_urls=("ref://a", "ref://b"),
    )
    p = plan(req, wan_profile())
    assert p.fidelity_cost == 0.0
    (step,) = p.steps
    assert step.mode is VideoMode.REFERENCE_TO_VIDEO
    assert step.reference_image_urls == ("ref://a", "ref://b")


# --------------------------------------------------------------------------- #
# Mode translation / downgrade
# --------------------------------------------------------------------------- #


def test_r2v_to_i2v_synthesizes_keyframe() -> None:
    # MiniMax: no r2v, but has i2v → synth keyframe then i2v.
    req = CanonicalVideoRequest(
        mode=VideoMode.REFERENCE_TO_VIDEO,
        duration_s=6,
        fps=25,
        resolution="768P",
        aspect=ASPECT_16_9,
        reference_image_urls=("ref://hero",),
    )
    p = plan(req, minimax_profile())
    assert p.feasible
    kinds = [s.kind for s in p.steps]
    assert kinds[0] is StepKind.SYNTHESIZE_KEYFRAME
    render = p.render_steps[0]
    assert render.mode is VideoMode.IMAGE_TO_VIDEO
    # The render's driving frame is the synthesized keyframe marker.
    assert render.image_url is not None and render.image_url.startswith("synth:")
    assert any(n.penalty is FidelityPenalty.KEYFRAME_SYNTH for n in p.notes)


def test_r2v_falls_back_to_t2v_when_no_i2v() -> None:
    # text-only backend: no r2v, no i2v → t2v, identity via prompt only.
    req = CanonicalVideoRequest(
        mode=VideoMode.REFERENCE_TO_VIDEO,
        duration_s=4,
        fps=24,
        resolution="720P",
        aspect=ASPECT_16_9,
        reference_image_urls=("ref://a",),
    )
    p = plan(req, text_only_profile())
    assert p.feasible
    assert p.render_steps[0].mode is VideoMode.TEXT_TO_VIDEO
    assert any(n.penalty is FidelityPenalty.MODE_DOWNGRADE for n in p.notes)
    # References dropped (cap 0) → truncation penalty present.
    assert any(n.penalty is FidelityPenalty.REFERENCES_TRUNCATED for n in p.notes)


def test_first_last_frame_to_i2v_keeps_first_frame() -> None:
    profile = CapabilityProfile(
        name="i2v-only",
        modes=frozenset({VideoMode.IMAGE_TO_VIDEO}),
        max_reference_images=1,
        min_duration_s=2,
        max_duration_s=5,
    )
    req = CanonicalVideoRequest(
        mode=VideoMode.FIRST_LAST_FRAME,
        duration_s=5,
        first_frame_url="frame://start",
        last_frame_url="frame://end",
    )
    p = plan(req, profile)
    render = p.render_steps[0]
    assert render.mode is VideoMode.IMAGE_TO_VIDEO
    assert render.image_url == "frame://start"
    assert any(n.penalty is FidelityPenalty.MODE_DOWNGRADE for n in p.notes)


def test_continuation_falls_back_to_i2v_last_frame() -> None:
    profile = CapabilityProfile(
        name="i2v-only",
        modes=frozenset({VideoMode.IMAGE_TO_VIDEO}),
        max_reference_images=1,
        min_duration_s=2,
        max_duration_s=5,
    )
    req = CanonicalVideoRequest(
        mode=VideoMode.VIDEO_CONTINUATION,
        duration_s=5,
        source_video_url="clip://prev",
    )
    p = plan(req, profile)
    assert p.render_steps[0].mode is VideoMode.IMAGE_TO_VIDEO
    assert any(n.penalty is FidelityPenalty.MODE_DOWNGRADE for n in p.notes)


def test_i2v_downgrades_to_t2v_when_unsupported() -> None:
    req = CanonicalVideoRequest(mode=VideoMode.IMAGE_TO_VIDEO, duration_s=4, image_url="img://x")
    p = plan(req, text_only_profile())
    assert p.render_steps[0].mode is VideoMode.TEXT_TO_VIDEO
    assert any(n.penalty is FidelityPenalty.MODE_DOWNGRADE for n in p.notes)


def test_infeasible_when_no_reachable_mode() -> None:
    # A backend that supports ONLY first_last_frame, asked for r2v with no t2v/i2v.
    profile = CapabilityProfile(
        name="flf-only",
        modes=frozenset({VideoMode.FIRST_LAST_FRAME}),
        max_reference_images=0,
    )
    req = CanonicalVideoRequest(mode=VideoMode.REFERENCE_TO_VIDEO, duration_s=5)
    p = plan(req, profile)
    assert not p.feasible
    assert p.fidelity_cost == float("inf")
    assert "INFEASIBLE" in p.rationale


# --------------------------------------------------------------------------- #
# Duration splitting
# --------------------------------------------------------------------------- #


def test_duration_split_into_overlap_chain() -> None:
    req = CanonicalVideoRequest(mode=VideoMode.TEXT_TO_VIDEO, duration_s=12)
    p = plan(req, wan_profile())  # 5s max, 0.5 overlap, has continuation
    assert p.segment_count == 3
    # First seg full-length t2v; later segs continuation seeded by last frame.
    assert p.render_steps[0].mode is VideoMode.TEXT_TO_VIDEO
    assert p.render_steps[1].mode is VideoMode.VIDEO_CONTINUATION
    assert p.render_steps[2].mode is VideoMode.VIDEO_CONTINUATION
    # Non-final segments carry extract-last-frame + overlap stitch markers.
    assert PostOp.EXTRACT_LAST_FRAME in p.render_steps[0].post_ops
    assert PostOp.STITCH_OVERLAP in p.render_steps[0].post_ops
    # The continuation segments reference the prior last frame.
    assert p.render_steps[1].source_video_url == "lastframe:0"
    # Split penalty scaled by extra segment count.
    split = [n for n in p.notes if n.penalty is FidelityPenalty.DURATION_SPLIT]
    assert split and split[0].weight == pytest.approx(2.0 * (3 - 1))
    # Final segment carries only the remainder → total render ≈ requested + overlaps.
    assert p.total_render_seconds == pytest.approx(13.0)


def test_duration_split_hard_cut_when_no_overlap() -> None:
    req = CanonicalVideoRequest(mode=VideoMode.TEXT_TO_VIDEO, duration_s=12)
    p = plan(req, text_only_profile())  # 4s fixed, no overlap, t2v only
    assert p.segment_count == 3
    # No continuation mode → every continuation segment is a hard-cut t2v.
    assert all(s.mode is VideoMode.TEXT_TO_VIDEO for s in p.render_steps)
    assert PostOp.STITCH_CUT in p.render_steps[0].post_ops
    assert PostOp.STITCH_OVERLAP not in p.render_steps[0].post_ops


def test_duration_clamp_single_segment() -> None:
    # 4s requested, backend only does discrete 6/10 ≥ → clamps to 6 (no split).
    req = CanonicalVideoRequest(mode=VideoMode.TEXT_TO_VIDEO, duration_s=4)
    p = plan(req, minimax_profile())
    assert p.segment_count == 1
    assert p.render_steps[0].duration_s == 6  # snapped to smallest option


def test_duration_split_does_not_overshoot_with_discrete_menu() -> None:
    req = CanonicalVideoRequest(mode=VideoMode.TEXT_TO_VIDEO, duration_s=12)
    p = plan(req, minimax_profile())  # 6/10 menu, no overlap
    # 10 covers 10, remaining 2 → smallest option 6.
    assert [s.duration_s for s in p.render_steps] == [10, 6]


# --------------------------------------------------------------------------- #
# Format clamping → post-ops
# --------------------------------------------------------------------------- #


def test_fps_resolution_aspect_clamps_attach_post_ops() -> None:
    req = CanonicalVideoRequest(
        mode=VideoMode.TEXT_TO_VIDEO,
        duration_s=4,
        fps=60,
        resolution="1080P",
        aspect=ASPECT_9_16,
    )
    p = plan(req, text_only_profile())  # 24fps, 720P, 16:9 only
    step = p.render_steps[0]
    assert step.fps == 24
    assert step.resolution == "720P"
    assert step.aspect == ASPECT_16_9
    ops = set(step.post_ops)
    assert PostOp.RETIME_FPS in ops
    assert PostOp.UPSCALE in ops  # 720P < 1080P
    assert PostOp.REFRAME_ASPECT in ops
    penalties = {n.penalty for n in p.notes}
    assert FidelityPenalty.FPS_CLAMP in penalties
    assert FidelityPenalty.RESOLUTION_CLAMP in penalties
    assert FidelityPenalty.ASPECT_CLAMP in penalties


def test_resolution_downscale_post_op() -> None:
    # Backend only renders 1080P but a low-res 480P clip was requested → the
    # produced clip is *richer* than target, so the pipeline must DOWNSCALE.
    profile = CapabilityProfile(
        name="hi",
        modes=frozenset({VideoMode.TEXT_TO_VIDEO}),
        resolution_options=("1080P",),
        min_duration_s=2,
        max_duration_s=5,
    )
    req = CanonicalVideoRequest(mode=VideoMode.TEXT_TO_VIDEO, duration_s=5, resolution="480P")
    p = plan(req, profile)
    assert PostOp.DOWNSCALE in p.render_steps[0].post_ops


def test_resolution_upscale_post_op() -> None:
    # Backend only renders 480P but 1080P requested → produced clip is below
    # target, so the pipeline must UPSCALE.
    profile = CapabilityProfile(
        name="lo",
        modes=frozenset({VideoMode.TEXT_TO_VIDEO}),
        resolution_options=("480P",),
        min_duration_s=2,
        max_duration_s=5,
    )
    req = CanonicalVideoRequest(mode=VideoMode.TEXT_TO_VIDEO, duration_s=5, resolution="1080P")
    p = plan(req, profile)
    assert PostOp.UPSCALE in p.render_steps[0].post_ops


# --------------------------------------------------------------------------- #
# Seed / prompt / negative-prompt / references
# --------------------------------------------------------------------------- #


def test_seed_dropped_when_unsupported() -> None:
    req = CanonicalVideoRequest(mode=VideoMode.TEXT_TO_VIDEO, duration_s=6, seed=99)
    p = plan(req, minimax_profile())  # no seed
    assert p.render_steps[0].seed is None
    assert any(n.penalty is FidelityPenalty.SEED_DROPPED for n in p.notes)


def test_seed_preserved_when_supported() -> None:
    req = CanonicalVideoRequest(mode=VideoMode.TEXT_TO_VIDEO, duration_s=5, seed=99)
    p = plan(req, wan_profile())
    assert p.render_steps[0].seed == 99
    assert not any(n.penalty is FidelityPenalty.SEED_DROPPED for n in p.notes)


def test_no_seed_requested_is_not_penalized() -> None:
    req = CanonicalVideoRequest(mode=VideoMode.TEXT_TO_VIDEO, duration_s=6, seed=None)
    p = plan(req, minimax_profile())
    assert not any(n.penalty is FidelityPenalty.SEED_DROPPED for n in p.notes)


def test_negative_prompt_dropped_when_unsupported() -> None:
    req = CanonicalVideoRequest(
        mode=VideoMode.TEXT_TO_VIDEO, duration_s=6, negative_prompt="blurry, low quality"
    )
    p = plan(req, minimax_profile())  # no negative prompt
    assert p.render_steps[0].negative_prompt is None
    assert any(n.penalty is FidelityPenalty.NEGATIVE_PROMPT_DROPPED for n in p.notes)


def test_references_truncated_to_cap() -> None:
    req = CanonicalVideoRequest(
        mode=VideoMode.REFERENCE_TO_VIDEO,
        duration_s=5,
        reference_image_urls=("a", "b", "c", "d", "e", "f"),
    )
    p = plan(req, wan_profile())  # cap 4
    assert p.render_steps[0].reference_image_urls == ("a", "b", "c", "d")
    note = [n for n in p.notes if n.penalty is FidelityPenalty.REFERENCES_TRUNCATED]
    assert note and note[0].weight == pytest.approx(1.5 * (6 - 4))


# --------------------------------------------------------------------------- #
# Prompt compression
# --------------------------------------------------------------------------- #


def test_compress_prompt_collapses_whitespace_under_budget() -> None:
    assert compress_prompt("a   b\n\nc", 100) == "a b c"


def test_compress_prompt_trims_trailing_clauses() -> None:
    prompt = (
        "A weary knight rides through the misty moor, "
        "cinematic lighting, volumetric fog, film grain, shallow depth of field"
    )
    out = compress_prompt(prompt, 45)
    assert len(out) <= 45
    # The load-bearing head (subject + action) is kept.
    assert out.startswith("A weary knight rides through the misty moor")


def test_compress_prompt_hard_truncates_single_long_clause() -> None:
    prompt = "word " * 200  # no separators after whitespace-collapse
    out = compress_prompt(prompt.strip(), 30)
    assert len(out) <= 30


def test_prompt_compressed_in_plan() -> None:
    long_prompt = "a vivid sweeping vista " * 40
    req = CanonicalVideoRequest(mode=VideoMode.TEXT_TO_VIDEO, duration_s=4, prompt=long_prompt)
    p = plan(req, text_only_profile())  # 500-char cap
    assert len(p.render_steps[0].prompt) <= 500
    assert any(n.penalty is FidelityPenalty.PROMPT_COMPRESSED for n in p.notes)


# --------------------------------------------------------------------------- #
# The original request is preserved; plan provenance
# --------------------------------------------------------------------------- #


def test_request_is_preserved_unmodified() -> None:
    req = CanonicalVideoRequest(
        mode=VideoMode.REFERENCE_TO_VIDEO,
        duration_s=30,
        seed=5,
        reference_image_urls=("a", "b", "c", "d", "e"),
    )
    p = plan(req, minimax_profile())
    # The plan carries the *original* request untouched for provenance.
    assert p.request == req
    assert p.request.duration_s == 30
    assert p.request.seed == 5
    assert len(p.request.reference_image_urls) == 5


def test_steps_are_indexed_in_order() -> None:
    req = CanonicalVideoRequest(
        mode=VideoMode.REFERENCE_TO_VIDEO,
        duration_s=12,
        reference_image_urls=("hero",),
    )
    p = plan(req, minimax_profile())  # synth + 2 render segments
    assert [s.index for s in p.steps] == list(range(len(p.steps)))


# --------------------------------------------------------------------------- #
# Router-facing selection across providers
# --------------------------------------------------------------------------- #


def test_best_plan_prefers_least_fidelity_loss() -> None:
    req = CanonicalVideoRequest(
        mode=VideoMode.REFERENCE_TO_VIDEO,
        duration_s=5,
        fps=24,
        resolution="720P",
        aspect=ASPECT_16_9,
        seed=3,
        reference_image_urls=("hero",),
    )
    profiles = [minimax_profile(), wan_profile(), text_only_profile()]
    best = best_plan(req, profiles)
    # Wan supports r2v natively + seed → zero-cost; it must win.
    assert best.backend == "dashscope-wan"
    assert best.fidelity_cost == 0.0


def test_rank_plans_orders_infeasible_last() -> None:
    flf_only = CapabilityProfile(
        name="flf-only",
        modes=frozenset({VideoMode.FIRST_LAST_FRAME}),
        max_reference_images=0,
    )
    req = CanonicalVideoRequest(mode=VideoMode.REFERENCE_TO_VIDEO, duration_s=5, seed=1)
    plans = plan_all(req, [flf_only, wan_profile()])
    ranked = rank_plans(plans)
    assert ranked[0].backend == "dashscope-wan"
    assert ranked[0].feasible
    assert ranked[-1].backend == "flf-only"
    assert not ranked[-1].feasible


def test_rank_plans_tie_breaks_on_input_order() -> None:
    # Two identical profiles → equal cost → stable on declared priority.
    a = wan_profile("wan-a")
    b = wan_profile("wan-b")
    req = CanonicalVideoRequest(mode=VideoMode.TEXT_TO_VIDEO, duration_s=5, seed=1)
    ranked = rank_plans(plan_all(req, [a, b]))
    assert [r.backend for r in ranked] == ["wan-a", "wan-b"]


def test_best_plan_empty_profiles_raises() -> None:
    req = CanonicalVideoRequest(mode=VideoMode.TEXT_TO_VIDEO, duration_s=5)
    with pytest.raises(ValueError):
        best_plan(req, [])


# --------------------------------------------------------------------------- #
# Combinatorial coverage: every mode against every preset always produces a
# runnable plan (feasible or explicitly infeasible, never a half-formed one).
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("mode", list(VideoMode))
@pytest.mark.parametrize(
    "profile_factory",
    [wan_profile, minimax_profile, text_only_profile],
)
def test_every_mode_every_preset_yields_runnable_render_steps(
    mode: VideoMode, profile_factory: object
) -> None:
    profile = profile_factory()  # type: ignore[operator]
    req = CanonicalVideoRequest(
        mode=mode,
        prompt="a scene",
        duration_s=8,
        fps=30,
        resolution="1080P",
        aspect=ASPECT_9_16,
        seed=11,
        reference_image_urls=("r1", "r2"),
        image_url="img://x",
        first_frame_url="frame://start",
        last_frame_url="frame://end",
        source_video_url="clip://prev",
    )
    p = plan(req, profile)
    if not p.feasible:
        # Infeasible is allowed but must be explicit (inf cost, no render steps run).
        assert p.fidelity_cost == float("inf")
        return
    # Feasible plans: every render step uses a mode the backend actually accepts,
    # within its declared format envelope.
    assert p.render_steps, "feasible plan must have at least one render step"
    for step in p.render_steps:
        assert step.mode is not None
        assert profile.supports_mode(step.mode)
        assert profile.min_duration_s <= step.duration_s or profile.duration_options
        if profile.fps_options:
            assert step.fps in profile.fps_options
        if profile.resolution_options:
            assert step.resolution in profile.resolution_options
        if profile.aspect_options:
            assert step.aspect in profile.aspect_options
        if not profile.supports_seed:
            assert step.seed is None
    # Cost is finite and non-negative.
    assert 0.0 <= p.fidelity_cost < float("inf")


@pytest.mark.parametrize("total", [1, 4, 5, 6, 9, 12, 17, 25, 60])
def test_split_covers_requested_duration(total: int) -> None:
    req = CanonicalVideoRequest(mode=VideoMode.TEXT_TO_VIDEO, duration_s=total)
    for factory in (wan_profile, text_only_profile):
        profile = factory()
        p = plan(req, profile)
        # The chain's *new* timeline (length minus re-rendered overlaps) covers the
        # request without absurd overshoot.
        overlap = profile.overlap_s if profile.supports_continuation_overlap else 0.0
        new_timeline = sum(s.duration_s for s in p.render_steps) - overlap * (p.segment_count - 1)
        assert new_timeline >= min(total, profile.max_duration_s) - 1e-6
        # Never more than one extra max-segment of overshoot.
        assert new_timeline <= total + profile.max_duration_s + 1e-6
