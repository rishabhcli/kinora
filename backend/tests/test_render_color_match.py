"""Shot-to-shot color matching (kinora.md §9.6): pure correction derivation +
filter assembly + a real ffmpeg stat measurement and a colour-matched stitch.
ffmpeg-backed tests skip when no binary."""

from __future__ import annotations

import pytest

from app.render import color_match, degrade
from app.render.color_match import (
    ColorCorrection,
    ColorStats,
    derive_correction,
    grade_filter,
    measure_stats,
    plan_scene_grades,
)
from app.render.stitch import concat_clips
from tests.test_render_support import png_bytes, wav_bytes

# --------------------------------------------------------------------------- #
# Pure correction derivation
# --------------------------------------------------------------------------- #


def test_darker_clip_gets_positive_brightness() -> None:
    ref = ColorStats(y=180.0, u=128.0, v=128.0)
    dark = ColorStats(y=120.0, u=128.0, v=128.0)
    correction = derive_correction(dark, ref)
    assert correction.brightness > 0  # lift the dark clip toward the reference
    assert correction.warm_shift == 0.0  # chroma already matched


def test_brighter_clip_gets_negative_brightness() -> None:
    ref = ColorStats(y=120.0, u=128.0, v=128.0)
    bright = ColorStats(y=200.0, u=128.0, v=128.0)
    assert derive_correction(bright, ref).brightness < 0


def test_cool_clip_warmed_toward_warm_reference() -> None:
    # Warm ref: V high (red), U low (blue). Cool clip: opposite.
    ref = ColorStats(y=128.0, u=110.0, v=150.0)  # warm
    cool = ColorStats(y=128.0, u=150.0, v=110.0)  # cool
    correction = derive_correction(cool, ref)
    assert correction.warm_shift > 0  # push the cool clip warmer


def test_correction_is_clamped_gentle() -> None:
    ref = ColorStats(y=255.0, u=0.0, v=255.0)
    clip = ColorStats(y=0.0, u=255.0, v=0.0)  # maximally different
    correction = derive_correction(clip, ref)
    assert abs(correction.brightness) <= color_match._MAX_BRIGHTNESS
    assert abs(correction.warm_shift) <= color_match._MAX_CHANNEL_GAIN


def test_invalid_stats_yield_identity() -> None:
    ref = ColorStats(y=180.0, u=128.0, v=128.0)
    assert derive_correction(ColorStats(0.0, 0.0, 0.0), ref).is_identity
    assert derive_correction(ref, ColorStats(0.0, 0.0, 0.0)).is_identity


def test_matched_clip_is_identity_noop() -> None:
    stats = ColorStats(y=140.0, u=128.0, v=128.0)
    correction = derive_correction(stats, stats)
    assert correction.is_identity
    assert grade_filter(correction) is None


def test_grade_filter_builds_eq_and_colorbalance() -> None:
    correction = ColorCorrection(brightness=0.05, warm_shift=0.04)
    f = grade_filter(correction)
    assert f is not None
    assert "eq=brightness=0.05" in f
    assert "colorbalance=rm=0.04" in f and "bm=-0.04" in f


# --------------------------------------------------------------------------- #
# Real ffmpeg measurement + colour-matched stitch
# --------------------------------------------------------------------------- #

pytestmark_ffmpeg = pytest.mark.skipif(
    not degrade.ffmpeg_available(), reason="no ffmpeg binary available"
)


@pytestmark_ffmpeg
def test_measure_stats_reads_brightness_difference() -> None:
    # A near-black still vs a bright still — measured luma should differ.
    dark = degrade.ken_burns_over_image(png_bytes(320, 320), 0.6, size=(320, 320))
    bright = degrade.audio_text_card(0.6, size=(320, 320), bg_color="white")
    dark_stats = measure_stats(dark)
    bright_stats = measure_stats(bright)
    assert dark_stats.is_valid and bright_stats.is_valid
    assert bright_stats.y > dark_stats.y


@pytestmark_ffmpeg
def test_plan_scene_grades_reference_is_identity() -> None:
    a = degrade.ken_burns_over_image(png_bytes(320, 320), 0.6, size=(320, 320))
    b = degrade.audio_text_card(0.6, size=(320, 320), bg_color="gray")
    grades = plan_scene_grades([a, b])
    assert len(grades) == 2
    assert grades[0].is_identity  # the first clip is the reference


@pytestmark_ffmpeg
def test_concat_with_color_match_is_playable() -> None:
    # Mismatched clips: one dark Ken-Burns, one bright card. Colour-matched concat
    # must still produce one valid, vertical, playable mp4.
    dark = degrade.ken_burns_over_image(
        png_bytes(360, 640), 1.0, audio_bytes=wav_bytes(1.0), size=(360, 640)
    )
    bright = degrade.audio_text_card(1.2, audio_bytes=wav_bytes(1.2), bg_color="white")
    scene = concat_clips([dark, bright], color_match=True)
    info = degrade.inspect(scene)
    assert info.has_video is True and info.has_audio is True
    assert degrade.verify_playable(scene) is True
