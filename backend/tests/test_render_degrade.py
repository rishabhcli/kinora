"""The degradation ladder produces REAL, playable mp4s (kinora.md §4.4/§12.4).

These tests generate genuine artifacts with ffmpeg and verify them with ffprobe:
a Ken-Burns pan over a still must be a valid mp4 of ≈ the requested duration with
a video stream (and an audio stream when narration is supplied). This is the
committed degradation rung, not a fake fallback.
"""

from __future__ import annotations

import pytest

from app.render import degrade
from tests.test_render_support import png_bytes, wav_bytes

pytestmark = pytest.mark.skipif(not degrade.ffmpeg_available(), reason="no ffmpeg binary available")


def test_ken_burns_video_only_is_a_real_mp4() -> None:
    clip = degrade.ken_burns_over_image(png_bytes(1280, 720), 3.0)
    info = degrade.probe(clip)
    assert info.has_video is True
    assert info.has_audio is False
    assert info.video_codec == "h264"
    assert info.width == 1920 and info.height == 1080
    assert abs(info.duration_s - 3.0) < 0.3
    assert degrade.verify_playable(clip) is True


def test_ken_burns_with_audio_muxes_a_real_audio_stream() -> None:
    clip = degrade.ken_burns_over_image(png_bytes(1280, 720), 2.0, audio_bytes=wav_bytes(2.0))
    info = degrade.probe(clip)
    assert info.has_video is True
    assert info.has_audio is True
    assert info.audio_codec in {"aac", "mp4a"}
    assert abs(info.duration_s - 2.0) < 0.3


def test_audio_text_card_bottom_rung_is_playable() -> None:
    clip = degrade.audio_text_card(1.5, audio_bytes=wav_bytes(1.5))
    info = degrade.probe(clip)
    assert info.has_video is True
    assert info.has_audio is True
    assert abs(info.duration_s - 1.5) < 0.3


def test_extract_frames_returns_real_frames() -> None:
    clip = degrade.ken_burns_over_image(png_bytes(640, 360), 2.0)
    frames = degrade.extract_frames(clip, 4)
    assert len(frames) == 4
    assert all(frame[:8] == b"\x89PNG\r\n\x1a\n" for frame in frames)


def test_empty_or_bad_inputs_are_rejected() -> None:
    with pytest.raises(ValueError):
        degrade.ken_burns_over_image(b"", 2.0)
    with pytest.raises(ValueError):
        degrade.ken_burns_over_image(png_bytes(64, 64), 0.0)
