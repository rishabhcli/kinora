"""Pure plan-layer tests for app.video.normalize — NO ffmpeg, NO network.

Everything here is deterministic dict-in / args-out: the ffprobe JSON parser, the
aspect-fit geometry, the target builder, and the ffmpeg arg-list planners. These
run anywhere (they never resolve or invoke a binary), so they are the regression
net for the whole decision surface independent of whether ffmpeg is installed.
"""

from __future__ import annotations

import pytest

from app.video.normalize import (
    AspectStrategy,
    ColorTags,
    FocalPoint,
    LoudnessTarget,
    MediaInfo,
    NormalizationTarget,
    build_concat_reencode_args,
    build_last_frame_args,
    build_normalize_args,
    build_video_filter,
    parse_ffprobe_json,
    parse_rational,
    plan_crop_fit,
    plan_pad_fit,
    streams_are_uniform,
)
from app.video.normalize.plan import (
    build_concat_demux_args,
    build_last_frame_fallback_args,
    color_metadata_args,
    loudnorm_filter,
    video_encode_args,
)

# --------------------------------------------------------------------------- #
# ffprobe JSON parser
# --------------------------------------------------------------------------- #


def _probe_payload(
    *,
    width: int = 1920,
    height: int = 1080,
    fps: str = "30/1",
    pix_fmt: str = "yuv420p",
    vcodec: str = "h264",
    with_audio: bool = True,
    color_range: str = "tv",
    duration: str = "5.0",
    rotation: int | None = None,
    sar: str = "1:1",
) -> dict:
    video = {
        "index": 0,
        "codec_type": "video",
        "codec_name": vcodec,
        "width": width,
        "height": height,
        "pix_fmt": pix_fmt,
        "r_frame_rate": fps,
        "avg_frame_rate": fps,
        "color_range": color_range,
        "color_space": "bt709",
        "sample_aspect_ratio": sar,
        "duration": duration,
    }
    if rotation is not None:
        video["side_data_list"] = [{"side_data_type": "Display Matrix", "rotation": rotation}]
    streams = [video]
    if with_audio:
        streams.append(
            {
                "index": 1,
                "codec_type": "audio",
                "codec_name": "aac",
                "sample_rate": "48000",
                "channels": 2,
                "channel_layout": "stereo",
                "duration": duration,
            }
        )
    return {
        "format": {"format_name": "mov,mp4,m4a", "duration": duration, "bit_rate": "1500000"},
        "streams": streams,
    }


def test_parse_basic_landscape_clip() -> None:
    info = parse_ffprobe_json(_probe_payload())
    assert info.has_video and info.has_audio
    assert info.dimensions == (1920, 1080)
    assert info.fps == pytest.approx(30.0)
    assert info.video is not None and info.video.pixel_format == "yuv420p"
    assert info.video.color_range == "tv"
    assert info.audio is not None and info.audio.sample_rate == 48000
    assert info.duration_s == pytest.approx(5.0)


def test_parse_handles_missing_and_na_fields() -> None:
    payload = {
        "format": {},
        "streams": [
            {"codec_type": "video", "codec_name": "vp9", "width": "N/A", "r_frame_rate": "0/0"},
        ],
    }
    info = parse_ffprobe_json(payload)
    assert info.has_video and not info.has_audio
    assert info.dimensions is None  # width N/A → unknown geometry
    assert info.fps is None  # 0/0 → unknown rate, never a divide-by-zero
    assert info.duration_s == 0.0  # no format/stream duration


def test_parse_falls_back_to_stream_duration() -> None:
    payload = _probe_payload(duration="3.5")
    payload["format"].pop("duration")
    info = parse_ffprobe_json(payload)
    assert info.duration_s == pytest.approx(3.5)  # longest stream duration


def test_parse_applies_rotation_to_display_dimensions() -> None:
    info = parse_ffprobe_json(_probe_payload(width=1920, height=1080, rotation=-90))
    # -90 normalises to 270 → display swaps W/H.
    assert info.video is not None and info.video.rotation == 270
    assert info.dimensions == (1080, 1920)


def test_parse_rational_variants() -> None:
    assert parse_rational("30/1") == pytest.approx(30.0)
    assert parse_rational("30000/1001") == pytest.approx(29.97, abs=0.01)
    assert parse_rational("24") == pytest.approx(24.0)
    assert parse_rational("0/0") is None
    assert parse_rational("N/A") is None
    assert parse_rational(None) is None


def test_matches_target_detects_canonical_clip() -> None:
    canonical = parse_ffprobe_json(
        _probe_payload(width=720, height=1280, fps="30/1", vcodec="h264", color_range="tv")
    )
    assert canonical.matches_target(
        width=720,
        height=1280,
        fps=30,
        video_codec="libx264",  # encoder name maps to the h264 codec
        pixel_format="yuv420p",
        color_range="tv",
    )
    # A landscape clip never matches the vertical target.
    landscape = parse_ffprobe_json(_probe_payload(width=1920, height=1080))
    assert not landscape.matches_target(
        width=720, height=1280, fps=30, video_codec="libx264", pixel_format="yuv420p"
    )


# --------------------------------------------------------------------------- #
# Aspect-fit geometry
# --------------------------------------------------------------------------- #


def test_pad_fit_landscape_into_vertical_letterboxes() -> None:
    fit = plan_pad_fit((1920, 1080), (720, 1280))
    # Scaled to fit inside 720 wide → 720x405, padded top/bottom.
    assert fit.scaled_w == 720
    assert fit.scaled_h == 404  # even-rounded 405
    assert fit.pad_x == 0
    assert fit.pad_y > 0 and fit.needs_pad
    # Padding is centred (equal-ish bars).
    assert abs((fit.target_h - fit.scaled_h) - 2 * fit.pad_y) <= 2


def test_pad_fit_matching_aspect_needs_no_pad() -> None:
    fit = plan_pad_fit((720, 1280), (720, 1280))
    assert (fit.scaled_w, fit.scaled_h) == (720, 1280)
    assert not fit.needs_pad


def test_pad_fit_all_dimensions_even() -> None:
    fit = plan_pad_fit((1001, 563), (720, 1280))
    for v in (fit.scaled_w, fit.scaled_h, fit.pad_x, fit.pad_y):
        assert v % 2 == 0


def test_crop_fit_fills_and_centers() -> None:
    fit = plan_crop_fit((1920, 1080), (720, 1280))
    # Scaled to COVER 1280 tall → wider than 720, crop the horizontal overflow.
    assert fit.scaled_h == 1280
    assert fit.scaled_w >= 720
    assert fit.needs_crop
    assert 0 <= fit.crop_x <= fit.scaled_w - 720
    assert fit.crop_y == 0


def test_crop_fit_focal_point_biases_window_and_clamps() -> None:
    # Focal point hard left → crop window clamps to x=0 (never negative).
    left = plan_crop_fit((1920, 1080), (720, 1280), focal=FocalPoint(x=0.0, y=0.5))
    assert left.crop_x == 0
    # Focal point hard right → window clamps to the max in-bounds offset.
    right = plan_crop_fit((1920, 1080), (720, 1280), focal=FocalPoint(x=1.0, y=0.5))
    assert right.crop_x <= right.scaled_w - 720
    assert right.crop_x >= left.crop_x


def test_aspect_fit_degenerate_source_fills_frame() -> None:
    fit = plan_pad_fit((0, 0), (720, 1280))
    assert (fit.scaled_w, fit.scaled_h) == (720, 1280)
    assert not fit.needs_pad


# --------------------------------------------------------------------------- #
# Target construction
# --------------------------------------------------------------------------- #


def test_target_rejects_odd_yuv420p_dimensions() -> None:
    with pytest.raises(ValueError, match="even dimensions"):
        NormalizationTarget(width=721, height=1280)


def test_target_from_settings_reads_normalize_block() -> None:
    from app.core.config import Settings

    settings = Settings(dashscope_api_key="test")
    target = NormalizationTarget.from_settings(settings)
    assert target.dimensions == (720, 1280)
    assert target.fps == 30
    assert target.aspect is AspectStrategy.PAD
    assert target.video_codec == "libx264"
    assert target.color.range == "tv"
    assert not target.loudness.enabled  # default 0.0 LUFS = disabled


# --------------------------------------------------------------------------- #
# Video filter chain
# --------------------------------------------------------------------------- #


def test_video_filter_pad_chain_for_landscape() -> None:
    info = parse_ffprobe_json(_probe_payload(width=1920, height=1080))
    target = NormalizationTarget(width=720, height=1280, fps=30)
    plan = build_video_filter(info, target)
    assert plan.out_width == 720 and plan.out_height == 1280
    assert "scale=720:" in plan.vf
    assert "pad=720:1280:" in plan.vf  # letterboxed
    assert "fps=30" in plan.vf
    assert "setsar=1" in plan.vf
    assert "format=yuv420p" in plan.vf
    assert "setrange=tv" in plan.vf  # colour range converted


def test_video_filter_crop_chain_has_crop_not_pad() -> None:
    info = parse_ffprobe_json(_probe_payload(width=1920, height=1080))
    target = NormalizationTarget(width=720, height=1280, aspect=AspectStrategy.CROP)
    plan = build_video_filter(info, target)
    assert "crop=720:1280:" in plan.vf
    assert "pad=" not in plan.vf


def test_video_filter_stretch_forces_exact_dims() -> None:
    info = parse_ffprobe_json(_probe_payload(width=640, height=480))
    target = NormalizationTarget(width=720, height=1280, aspect=AspectStrategy.STRETCH)
    plan = build_video_filter(info, target)
    assert plan.vf.startswith("scale=720:1280")
    assert "pad=" not in plan.vf and "crop=" not in plan.vf


def test_video_filter_unknown_geometry_is_safe() -> None:
    # A clip with no probed video geometry → decrease-fit + pad always lands target.
    info = MediaInfo()
    target = NormalizationTarget(width=720, height=1280)
    plan = build_video_filter(info, target)
    assert "force_original_aspect_ratio=decrease" in plan.vf
    assert "pad=720:1280" in plan.vf


def test_color_metadata_and_loudnorm_helpers() -> None:
    args = color_metadata_args(ColorTags())
    assert "-color_primaries" in args and "bt709" in args
    assert "-color_range" in args and "tv" in args

    assert loudnorm_filter(LoudnessTarget(integrated_lufs=0.0)) is None  # disabled
    f = loudnorm_filter(LoudnessTarget(integrated_lufs=-16.0, true_peak=-1.5, loudness_range=11.0))
    assert f is not None and f.startswith("loudnorm=I=-16")


def test_video_encode_args_carries_x264_quality_knobs() -> None:
    target = NormalizationTarget(x264_preset="medium", crf=18)
    args = video_encode_args(target)
    assert args[:2] == ["-c:v", "libx264"]
    assert "-preset" in args and "medium" in args
    assert "-crf" in args and "18" in args
    assert "-color_primaries" in args  # colour metadata stamped on the encode


# --------------------------------------------------------------------------- #
# Full normalize invocation planning
# --------------------------------------------------------------------------- #


def test_normalize_args_for_clip_with_audio() -> None:
    info = parse_ffprobe_json(_probe_payload(with_audio=True))
    target = NormalizationTarget(width=720, height=1280)
    plan = build_normalize_args(
        ffmpeg="ffmpeg", in_path="in.mp4", out_path="out.mp4", info=info, target=target
    )
    assert plan.args[0] == "ffmpeg"
    assert plan.args[-1] == "out.mp4"
    assert not plan.synthesized_audio
    # Maps the source audio directly, no synthesised silence input.
    assert "0:a:0" in plan.args
    assert "anullsrc" not in " ".join(plan.args)
    assert "-movflags" in plan.args and "+faststart" in plan.args


def test_normalize_args_synthesizes_silence_for_video_only_clip() -> None:
    info = parse_ffprobe_json(_probe_payload(with_audio=False))
    target = NormalizationTarget(width=720, height=1280, audio_sample_rate=48000, audio_channels=2)
    plan = build_normalize_args(
        ffmpeg="ffmpeg", in_path="in.mp4", out_path="out.mp4", info=info, target=target
    )
    assert plan.synthesized_audio
    joined = " ".join(plan.args)
    assert "anullsrc=channel_layout=stereo:sample_rate=48000" in joined
    assert "-shortest" in plan.args  # silent track trimmed to the video


def test_normalize_args_apply_loudnorm_when_enabled() -> None:
    info = parse_ffprobe_json(_probe_payload(with_audio=True))
    target = NormalizationTarget(loudness=LoudnessTarget(integrated_lufs=-16.0))
    plan = build_normalize_args(
        ffmpeg="ffmpeg", in_path="in.mp4", out_path="out.mp4", info=info, target=target
    )
    assert "loudnorm=I=-16" in plan.filtergraph


# --------------------------------------------------------------------------- #
# Last-frame extraction planning
# --------------------------------------------------------------------------- #


def test_last_frame_args_use_end_seek_when_duration_known() -> None:
    args = build_last_frame_args(
        ffmpeg="ffmpeg", in_path="clip.mp4", out_path="last.png", duration_s=5.0
    )
    assert "-sseof" in args  # fast end-seek
    assert "-update" in args and "1" in args
    assert "-frames:v" in args
    assert args[-1] == "last.png"


def test_last_frame_args_no_seek_when_duration_unknown() -> None:
    args = build_last_frame_args(
        ffmpeg="ffmpeg", in_path="clip.mp4", out_path="last.png", duration_s=None
    )
    assert "-sseof" not in args  # cannot seek without a duration


def test_last_frame_fallback_is_full_decode() -> None:
    args = build_last_frame_fallback_args(ffmpeg="ffmpeg", in_path="c.webm", out_path="last.jpg")
    assert "-sseof" not in args
    assert "-update" in args and "-frames:v" in args


# --------------------------------------------------------------------------- #
# Concat planning + uniformity detection
# --------------------------------------------------------------------------- #


def test_streams_are_uniform_true_for_identical_clips() -> None:
    a = parse_ffprobe_json(_probe_payload())
    b = parse_ffprobe_json(_probe_payload())
    assert streams_are_uniform([a, b])
    assert streams_are_uniform([a])  # a single clip is trivially uniform


def test_streams_are_not_uniform_on_geometry_or_audio_mismatch() -> None:
    a = parse_ffprobe_json(_probe_payload(width=1920, height=1080))
    b = parse_ffprobe_json(_probe_payload(width=720, height=1280))
    assert not streams_are_uniform([a, b])  # geometry differs

    with_audio = parse_ffprobe_json(_probe_payload(with_audio=True))
    no_audio = parse_ffprobe_json(_probe_payload(with_audio=False))
    assert not streams_are_uniform([with_audio, no_audio])  # audio presence differs


def test_streams_not_uniform_when_a_clip_has_no_video() -> None:
    a = parse_ffprobe_json(_probe_payload())
    assert a.audio is not None
    audio_only = MediaInfo(streams=[a.audio])
    assert not streams_are_uniform([a, audio_only])


def test_concat_demux_args_stream_copy() -> None:
    args = build_concat_demux_args(ffmpeg="ffmpeg", list_path="list.txt", out_path="out.mp4")
    assert "-f" in args and "concat" in args
    assert "-safe" in args and "0" in args
    assert "-c" in args and "copy" in args  # no re-encode


def test_concat_reencode_args_build_filtergraph() -> None:
    target = NormalizationTarget(width=720, height=1280)
    plan = build_concat_reencode_args(
        ffmpeg="ffmpeg",
        in_paths=["a.mp4", "b.mp4", "c.mp4"],
        out_path="out.mp4",
        target=target,
    )
    assert plan.reencoded
    assert "concat=n=3:v=1:a=1" in plan.filtergraph
    assert "dynaudnorm" in plan.filtergraph  # audio level-normalised at the join
    # One -i per input.
    assert plan.args.count("-i") == 3
    assert "-c:v" in plan.args and "libx264" in plan.args
