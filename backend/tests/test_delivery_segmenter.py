"""The packaging *plan* layer — exact ffmpeg arg lists + byte ranges, no ffmpeg run."""

from __future__ import annotations

import pytest

from app.delivery.errors import SegmentationError
from app.delivery.ladder import Rendition
from app.delivery.profiles import NormalizationSpec, normalization_spec, profile_for
from app.delivery.segmenter import (
    build_dash_segmenting_plan,
    build_encode_plan,
    build_hls_segmenting_plan,
    plan_byte_ranges,
)


def _rendition() -> Rendition:
    return Rendition(
        name="720p", width=720, height=1280, video_bitrate_kbps=2800, max_bitrate_kbps=4200
    )


def _spec(seg: float = 2.0) -> NormalizationSpec:
    return normalization_spec(profile_for("minimax"), fps=30, segment_duration_s=seg)


def test_encode_plan_has_closed_gop_and_forced_keyframes() -> None:
    plan = build_encode_plan(
        source="in.mp4", rendition=_rendition(), spec=_spec(), output="out.mp4"
    )
    args = plan.args
    assert args[0] == "ffmpeg"  # placeholder binary
    assert args[-1] == "out.mp4"
    # IDR alignment: -g == -keyint_min == gop, scene-cut disabled, keys forced.
    g_idx = args.index("-g")
    assert args[g_idx + 1] == "60"
    assert args[args.index("-keyint_min") + 1] == "60"
    assert args[args.index("-sc_threshold") + 1] == "0"
    assert "-force_key_frames" in args
    # Ladder bitrate knobs present.
    assert args[args.index("-b:v") + 1] == "2800k"
    assert args[args.index("-maxrate") + 1] == "4200k"
    assert args[args.index("-pix_fmt") + 1] == "yuv420p"
    # H.264 + AAC target codecs.
    assert args[args.index("-c:v") + 1] == "libx264"
    assert args[args.index("-c:a") + 1] == "aac"


def test_encode_plan_video_filter_scales_pads_and_conforms_fps() -> None:
    plan = build_encode_plan(source="in.mp4", rendition=_rendition(), spec=_spec(), output="o.mp4")
    vf = plan.args[plan.args.index("-vf") + 1]
    assert "scale=720:1280:force_original_aspect_ratio=decrease" in vf
    assert "pad=720:1280" in vf
    assert "fps=30" in vf
    assert "setsar=1" in vf


def test_encode_plan_with_binary_substitutes_slot_zero() -> None:
    plan = build_encode_plan(source="in.mp4", rendition=_rendition(), spec=_spec(), output="o.mp4")
    real = plan.with_binary("/usr/bin/ffmpeg")
    assert real[0] == "/usr/bin/ffmpeg"
    assert real[1:] == plan.args[1:]


def test_hls_segmenting_plan_is_fmp4_cmaf() -> None:
    durations = [2.0, 2.0, 1.0]
    plan = build_hls_segmenting_plan(
        source="in.mp4",
        rendition=_rendition(),
        spec=_spec(),
        segment_durations=durations,
        segment_dir="/out/720p",
    )
    args = plan.args
    assert args[args.index("-f") + 1] == "hls"
    assert args[args.index("-hls_segment_type") + 1] == "fmp4"
    assert args[args.index("-hls_time") + 1] == "2"
    assert plan.expected_segment_count == 3
    assert plan.segment_durations == [2.0, 2.0, 1.0]
    assert plan.init_output == "/out/720p/init.mp4"
    assert plan.media_playlist == "/out/720p/media.m3u8"
    # The fused plan must not keep +faststart (HLS fmp4 doesn't want a relocated moov).
    assert "-movflags" not in args


def test_hls_segmenting_plan_rejects_empty_durations() -> None:
    with pytest.raises(SegmentationError):
        build_hls_segmenting_plan(
            source="in.mp4",
            rendition=_rendition(),
            spec=_spec(),
            segment_durations=[],
            segment_dir="/out",
        )


def test_dash_segmenting_plan_maps_every_rendition() -> None:
    r1 = _rendition()
    r2 = Rendition(
        name="360p", width=360, height=640, video_bitrate_kbps=900, max_bitrate_kbps=1300
    )
    plan = build_dash_segmenting_plan(
        sources_by_rendition=[(r1, "r1.mp4"), (r2, "r2.mp4")],
        spec=_spec(),
        segment_durations=[2.0, 1.0],
        out_mpd="/out/manifest.mpd",
    )
    args = plan.args
    assert args[args.index("-f") + 1] == "dash"
    assert args.count("-i") == 2  # two rendition inputs
    assert args.count("-map") == 4  # video+audio per input
    assert args[args.index("-use_template") + 1] == "1"
    assert args[args.index("-use_timeline") + 1] == "1"
    assert plan.media_playlist == "/out/manifest.mpd"


def test_dash_segmenting_plan_rejects_no_renditions() -> None:
    with pytest.raises(SegmentationError):
        build_dash_segmenting_plan(
            sources_by_rendition=[], spec=_spec(), segment_durations=[2.0], out_mpd="m.mpd"
        )


def test_plan_byte_ranges_contiguous_with_init() -> None:
    init, ranges = plan_byte_ranges([100, 200, 50], init_size=40)
    assert init is not None
    assert (init.offset, init.length) == (0, 40)
    assert [(r.offset, r.length) for r in ranges] == [(40, 100), (140, 200), (340, 50)]
    # HLS + HTTP serialisations.
    assert ranges[1].hls == "200@140"
    assert ranges[1].http == "bytes=140-339"
    assert ranges[1].end == 339


def test_plan_byte_ranges_no_init() -> None:
    init, ranges = plan_byte_ranges([10, 20])
    assert init is None
    assert [(r.offset, r.length) for r in ranges] == [(0, 10), (10, 20)]


def test_plan_byte_ranges_rejects_zero_and_negative() -> None:
    with pytest.raises(SegmentationError):
        plan_byte_ranges([10, 0])
    with pytest.raises(SegmentationError):
        plan_byte_ranges([-1])
    with pytest.raises(SegmentationError):
        plan_byte_ranges([10], init_size=-5)
