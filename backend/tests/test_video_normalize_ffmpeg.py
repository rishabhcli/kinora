"""Integration tests for app.video.normalize — REAL ffmpeg, NO network.

Gated on ffmpeg presence (skips cleanly in a CI without it). Real clips are built
with the existing degrade Ken-Burns helpers / Pillow PNGs (same builders the
render suite uses); MiniMax/Wan clips are simulated by producing clips at
different geometries / fps / codecs / colour ranges with ffmpeg directly, then
asserting the normalizer makes them all interchangeable.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from app.video.normalize import (
    AspectStrategy,
    ClipProbe,
    ColorTags,
    LoudnessTarget,
    NormalizationTarget,
    Normalizer,
    concat_clips,
    extract_last_frame,
    ffmpeg_available,
)
from app.video.normalize.runtime import get_ffmpeg_exe, run
from tests.test_render_support import png_bytes, wav_bytes

pytestmark = pytest.mark.skipif(not ffmpeg_available(), reason="no ffmpeg binary available")


# --------------------------------------------------------------------------- #
# Real clip builders (vary the shape so we exercise the normalizer)
# --------------------------------------------------------------------------- #


def _make_clip(
    *,
    width: int,
    height: int,
    fps: int = 24,
    duration_s: float = 1.0,
    vcodec: str = "libx264",
    pix_fmt: str = "yuv420p",
    color_range: str | None = None,
    with_audio: bool = True,
    container_ext: str = "mp4",
) -> bytes:
    """Build a real test clip with arbitrary geometry/codec/fps via ffmpeg."""
    ffmpeg = get_ffmpeg_exe()
    with tempfile.TemporaryDirectory(prefix="kinora_mkclip_") as tmp:
        tmp_dir = Path(tmp)
        out = tmp_dir / f"clip.{container_ext}"
        args = [
            ffmpeg,
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"testsrc=size={width}x{height}:rate={fps}:duration={duration_s:g}",
        ]
        if with_audio:
            args += [
                "-f",
                "lavfi",
                "-i",
                f"sine=frequency=440:duration={duration_s:g}",
            ]
        args += ["-c:v", vcodec, "-pix_fmt", pix_fmt, "-r", str(fps)]
        if color_range:
            args += ["-color_range", color_range]
        if with_audio:
            args += ["-c:a", "aac", "-shortest"]
        else:
            args += ["-an"]
        args += [str(out)]
        run(args, timeout=120.0)
        return out.read_bytes()


def _target(width: int = 720, height: int = 1280, fps: int = 30) -> NormalizationTarget:
    return NormalizationTarget(width=width, height=height, fps=fps)


# --------------------------------------------------------------------------- #
# ClipProbe round-trip
# --------------------------------------------------------------------------- #


def test_clip_probe_reports_real_geometry_and_audio() -> None:
    clip = _make_clip(width=1280, height=720, fps=24, duration_s=1.0, with_audio=True)
    info = ClipProbe().probe_bytes(clip)
    assert info.dimensions == (1280, 720)
    assert info.fps == pytest.approx(24.0, abs=0.5)
    assert info.has_audio
    assert info.duration_s == pytest.approx(1.0, abs=0.3)


def test_clip_probe_falls_back_when_no_ffprobe(monkeypatch: pytest.MonkeyPatch) -> None:
    clip = _make_clip(width=640, height=360, fps=30, duration_s=1.0, with_audio=True)
    import app.video.normalize.probe as probe_mod

    monkeypatch.setattr(probe_mod, "get_ffprobe_exe", lambda: None)  # portable image
    info = ClipProbe().probe_bytes(clip)
    assert info.has_video
    assert info.dimensions == (640, 360)
    assert info.duration_s == pytest.approx(1.0, abs=0.3)


# --------------------------------------------------------------------------- #
# Normalizer — any input → canonical target
# --------------------------------------------------------------------------- #


def test_normalize_landscape_to_vertical_pads() -> None:
    clip = _make_clip(width=1920, height=1080, fps=24, duration_s=1.0)
    result = Normalizer(_target()).normalize_bytes(clip)
    assert (result.width, result.height) == (720, 1280)
    info = ClipProbe().probe_bytes(result.clip_bytes)
    assert info.dimensions == (720, 1280)
    assert info.fps == pytest.approx(30.0, abs=0.5)
    assert info.has_audio


def test_normalize_synthesizes_audio_for_silent_clip() -> None:
    clip = _make_clip(width=720, height=1280, fps=30, duration_s=1.0, with_audio=False)
    result = Normalizer(_target()).normalize_bytes(clip)
    assert result.synthesized_audio
    info = ClipProbe().probe_bytes(result.clip_bytes)
    assert info.has_audio  # uniform layout: silence where the source had none
    assert info.has_video


def test_normalize_unifies_two_different_provider_shapes() -> None:
    """The core promise: clips from different 'providers' become identical in shape."""
    target = _target()
    norm = Normalizer(target)
    # Simulate a Wan-ish landscape h264 clip and a MiniMax-ish square vp9 webm.
    wan = _make_clip(width=1280, height=720, fps=24, duration_s=1.0, vcodec="libx264")
    mini = _make_clip(
        width=480,
        height=480,
        fps=25,
        duration_s=1.0,
        vcodec="libvpx-vp9",
        with_audio=False,
        container_ext="webm",
    )
    a = ClipProbe().probe_bytes(norm.normalize_bytes(wan).clip_bytes)
    b = ClipProbe().probe_bytes(norm.normalize_bytes(mini).clip_bytes)
    assert a.dimensions == b.dimensions == (720, 1280)
    assert a.video is not None and b.video is not None
    assert a.video.codec_name == b.video.codec_name  # both h264
    assert a.video.pixel_format == b.video.pixel_format == "yuv420p"
    assert a.has_audio and b.has_audio  # both have a (real / synthesised) track


def test_normalize_passthrough_for_already_canonical_clip() -> None:
    # A clip already at the target geometry/fps/codec/pixfmt is returned verbatim.
    # The target imposes no colour-range requirement here, so a clip ffmpeg did
    # not stamp a range tag onto still counts as canonical (range=None matches any).
    target = NormalizationTarget(width=720, height=1280, fps=30, color=ColorTags(range=None))
    canonical = _make_clip(width=720, height=1280, fps=30, duration_s=1.0, with_audio=True)
    result = Normalizer(target).normalize_bytes(canonical)
    assert result.passthrough
    assert result.clip_bytes == canonical  # returned verbatim, no transcode

    # With a strict colour-range requirement the same (untagged) clip is NOT a
    # passthrough — it is transcoded so the canonical range tag is enforced.
    strict = NormalizationTarget(width=720, height=1280, fps=30, color=ColorTags(range="tv"))
    transcoded = Normalizer(strict).normalize_bytes(canonical)
    assert not transcoded.passthrough


def test_normalize_loudnorm_pass_produces_valid_clip() -> None:
    clip = _make_clip(width=720, height=1280, fps=30, duration_s=1.0, with_audio=True)
    target = NormalizationTarget(
        width=720, height=1280, fps=30, loudness=LoudnessTarget(integrated_lufs=-16.0)
    )
    result = Normalizer(target).normalize_bytes(clip)
    info = ClipProbe().probe_bytes(result.clip_bytes)
    assert info.has_video and info.has_audio  # loudnorm pass kept a valid track


def test_normalize_crop_strategy_fills_frame() -> None:
    clip = _make_clip(width=1920, height=1080, fps=24, duration_s=1.0)
    target = NormalizationTarget(width=720, height=1280, fps=30, aspect=AspectStrategy.CROP)
    result = Normalizer(target).normalize_bytes(clip)
    info = ClipProbe().probe_bytes(result.clip_bytes)
    assert info.dimensions == (720, 1280)


# --------------------------------------------------------------------------- #
# Universal last-frame extraction
# --------------------------------------------------------------------------- #


def test_extract_last_frame_from_mp4() -> None:
    clip = _make_clip(width=640, height=360, fps=24, duration_s=1.0, with_audio=False)
    frame = extract_last_frame(clip, image_format="png")
    assert frame[:8] == b"\x89PNG\r\n\x1a\n"  # a real PNG


def test_extract_last_frame_from_webm_container() -> None:
    clip = _make_clip(
        width=480, height=480, fps=25, duration_s=1.0, vcodec="libvpx-vp9",
        with_audio=False, container_ext="webm",
    )
    frame = extract_last_frame(clip, image_format="jpg")
    assert frame[:3] == b"\xff\xd8\xff"  # a real JPEG


def test_extract_last_frame_from_degrade_ken_burns_clip() -> None:
    from app.render import degrade

    clip = degrade.ken_burns_over_image(png_bytes(640, 360), 1.0, audio_bytes=wav_bytes(1.0))
    frame = extract_last_frame(clip)
    assert frame[:8] == b"\x89PNG\r\n\x1a\n"


# --------------------------------------------------------------------------- #
# Concatenation: copy fast path + safe re-encode fallback
# --------------------------------------------------------------------------- #


def test_concat_uniform_clips_stream_copies() -> None:
    target = _target()
    norm = Normalizer(target)
    # Two clips normalised to the SAME target are uniform → stream copy.
    a = norm.normalize_bytes(_make_clip(width=1280, height=720, duration_s=1.0)).clip_bytes
    b = norm.normalize_bytes(_make_clip(width=640, height=480, duration_s=1.0)).clip_bytes
    result = concat_clips([a, b], target=target, normalizer=norm)
    assert result.stream_copied
    assert not result.normalized_inputs
    info = ClipProbe().probe_bytes(result.clip_bytes)
    assert info.dimensions == (720, 1280)
    assert info.duration_s == pytest.approx(2.0, abs=0.5)


def test_concat_nonuniform_clips_reencode_safely() -> None:
    target = _target()
    # Raw, un-normalised clips of different geometry/codec → re-encode fallback.
    wan = _make_clip(width=1280, height=720, fps=24, duration_s=1.0, vcodec="libx264")
    mini = _make_clip(
        width=480, height=480, fps=25, duration_s=1.0, vcodec="libvpx-vp9",
        with_audio=False, container_ext="webm",
    )
    result = concat_clips([wan, mini], target=target)
    assert not result.stream_copied
    assert result.normalized_inputs
    info = ClipProbe().probe_bytes(result.clip_bytes)
    assert info.dimensions == (720, 1280)
    assert info.has_video and info.has_audio
    assert info.duration_s == pytest.approx(2.0, abs=0.6)


def test_concat_single_clip_passthrough() -> None:
    target = _target()
    clip = _make_clip(width=720, height=1280, fps=30, duration_s=1.0)
    result = concat_clips([clip], target=target)
    assert result.clip_count == 1
    assert result.clip_bytes == clip  # untouched without force_reencode


def test_concat_force_reencode_normalizes_single_clip() -> None:
    target = _target()
    clip = _make_clip(width=1920, height=1080, fps=24, duration_s=1.0)
    result = concat_clips([clip], target=target, force_reencode=True)
    info = ClipProbe().probe_bytes(result.clip_bytes)
    assert info.dimensions == (720, 1280)
