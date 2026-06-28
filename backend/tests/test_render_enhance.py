"""Frame-interpolation + upscaling hooks (kinora.md §9.2): pure plan decisions
(thresholds, no-op detection, filter assembly) + a real ffmpeg apply pass that
upscales/sharpens a small clip. ffmpeg-backed tests skip when no binary."""

from __future__ import annotations

import pytest

from app.render import degrade
from app.render.degrade import FILM_SIZE
from app.render.enhance import (
    EnhanceProfile,
    apply_enhancement,
    enhance_clip,
    plan_enhancement,
)
from tests.test_render_support import png_bytes, wav_bytes

# --------------------------------------------------------------------------- #
# Pure planning
# --------------------------------------------------------------------------- #


def test_plan_interpolates_low_fps() -> None:
    plan = plan_enhancement(source_fps=12.0, source_size=FILM_SIZE)
    assert plan.do_interpolate is True
    assert plan.target_fps == degrade.DEFAULT_FPS
    vf = plan.video_filter()
    assert vf is not None and "minterpolate=fps=30" in vf


def test_plan_skips_interpolation_near_target_fps() -> None:
    # 28 fps is within min_fps_gain (6) of 30 → not worth interpolating.
    plan = plan_enhancement(source_fps=28.0, source_size=FILM_SIZE)
    assert plan.do_interpolate is False


def test_plan_upscales_small_clip() -> None:
    plan = plan_enhancement(source_fps=30.0, source_size=(360, 640))
    assert plan.do_upscale is True
    vf = plan.video_filter()
    assert vf is not None and f"scale={FILM_SIZE[0]}:{FILM_SIZE[1]}" in vf
    assert "unsharp=" in vf  # sharpen applied with the upscale


def test_plan_noop_when_already_film_quality() -> None:
    plan = plan_enhancement(source_fps=30.0, source_size=FILM_SIZE)
    assert plan.is_noop is True
    assert plan.video_filter() is None


def test_plan_unknown_fps_never_interpolates() -> None:
    # 0 fps means "unknown" (the ffmpeg-stderr fallback) → never interpolate on a guess.
    plan = plan_enhancement(source_fps=0.0, source_size=(360, 640))
    assert plan.do_interpolate is False
    assert plan.do_upscale is True  # still upscales on the known small size


def test_passthrough_profile_is_always_noop() -> None:
    plan = plan_enhancement(
        source_fps=8.0, source_size=(160, 160), profile=EnhanceProfile.passthrough()
    )
    assert plan.is_noop is True


def test_sharpen_zero_disables_unsharp() -> None:
    profile = EnhanceProfile(sharpen_amount=0.0)
    plan = plan_enhancement(source_fps=30.0, source_size=(360, 640), profile=profile)
    vf = plan.video_filter()
    assert vf is not None and "unsharp" not in vf


# --------------------------------------------------------------------------- #
# Real ffmpeg apply
# --------------------------------------------------------------------------- #

pytestmark_ffmpeg = pytest.mark.skipif(
    not degrade.ffmpeg_available(), reason="no ffmpeg binary available"
)


@pytestmark_ffmpeg
def test_apply_upscales_small_clip_to_film_geometry() -> None:
    # A real small landscape clip with audio.
    small = degrade.ken_burns_over_image(
        png_bytes(360, 240), 1.0, audio_bytes=wav_bytes(1.0), size=(360, 240)
    )
    result = enhance_clip(small)
    assert result.changed is True
    info = degrade.inspect(result.clip_bytes)
    assert (info.width, info.height) == FILM_SIZE
    assert info.has_audio is True  # audio copied through
    assert degrade.verify_playable(result.clip_bytes) is True


@pytestmark_ffmpeg
def test_apply_noop_returns_clip_unchanged() -> None:
    clip = degrade.ken_burns_over_image(png_bytes(*FILM_SIZE), 1.0, size=FILM_SIZE)
    result = enhance_clip(clip)
    # Already at film geometry & 30fps → no-op, byte-identical pass-through.
    assert result.changed is False
    assert result.clip_bytes is clip


def test_apply_empty_clip_raises() -> None:
    plan = plan_enhancement(source_fps=10.0, source_size=(100, 100))
    with pytest.raises(ValueError, match="empty"):
        apply_enhancement(b"", plan)
