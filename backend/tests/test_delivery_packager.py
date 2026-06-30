"""ffmpeg-gated packager integration — real CMAF/HLS segmentation of a real clip.

The pure plan layer is tested without ffmpeg (test_delivery_segmenter.py); this
file actually runs ffmpeg, so it is skipped where no binary is available. It
produces a real source mp4 via the render degrade lane, packages it into a
rendition's CMAF init + ``.m4s`` fragments, and asserts the artifacts exist and
the HLS media playlist the muxer wrote references them.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from app.delivery.ladder import Rendition
from app.delivery.packager import AbrPackager
from app.delivery.profiles import normalization_spec, profile_for
from app.render import degrade
from tests.test_render_support import png_bytes, wav_bytes

pytestmark = pytest.mark.skipif(
    not degrade.ffmpeg_available(), reason="no ffmpeg binary available"
)


def _source_clip(duration_s: float = 4.0) -> bytes:
    return degrade.ken_burns_over_image(
        png_bytes(720, 1280),
        duration_s,
        audio_bytes=wav_bytes(duration_s),
        size=(720, 1280),
    )


def test_package_rendition_produces_cmaf_segments() -> None:
    rendition = Rendition(
        name="360p", width=360, height=640, video_bitrate_kbps=900, max_bitrate_kbps=1300
    )
    spec = normalization_spec(profile_for("ken_burns"), fps=30, segment_duration_s=2.0)
    packager = AbrPackager()
    with tempfile.TemporaryDirectory(prefix="kinora_abr_test_") as tmp:
        src = Path(tmp) / "src.mp4"
        src.write_bytes(_source_clip(4.0))
        result = packager.package_rendition(
            source=str(src),
            rendition=rendition,
            spec=spec,
            segment_durations=[2.0, 2.0],
            out_dir=tmp,
        )
        # Real CMAF init + at least one fragment were written to disk.
        assert Path(result.init_path).exists()
        assert result.segment_paths, "no .m4s segments were produced"
        for seg in result.segment_paths:
            assert Path(seg).exists()
            assert Path(seg).stat().st_size > 0
        # The muxer wrote the media playlist the plan declared.
        assert result.media_playlist_path is not None
        playlist = Path(result.media_playlist_path).read_text()
        assert "#EXTM3U" in playlist
        assert "#EXT-X-MAP" in playlist
        assert result.declared_segment_durations == [2.0, 2.0]


def test_package_rendition_segments_are_independently_playable() -> None:
    # The whole packaged output (init + fragments) must decode — proof the
    # CMAF segmentation produced real, playable media, not just files.
    rendition = Rendition(
        name="240p", width=240, height=428, video_bitrate_kbps=450, max_bitrate_kbps=700
    )
    spec = normalization_spec(profile_for("ken_burns"), fps=30, segment_duration_s=2.0)
    packager = AbrPackager()
    with tempfile.TemporaryDirectory(prefix="kinora_abr_play_") as tmp:
        src = Path(tmp) / "src.mp4"
        src.write_bytes(_source_clip(2.0))
        result = packager.package_rendition(
            source=str(src),
            rendition=rendition,
            spec=spec,
            segment_durations=[2.0],
            out_dir=tmp,
        )
        # Concatenate init + first fragment into a probeable mp4.
        init = Path(result.init_path).read_bytes()
        frag = Path(result.segment_paths[0]).read_bytes()
        assert degrade.verify_playable(init + frag) is True


def test_spec_for_resolves_profile_and_spec() -> None:
    profile, spec = AbrPackager.spec_for("minimax", fps=30, segment_duration_s=2.0)
    assert profile.key == "minimax"
    assert spec.gop_size == 60
