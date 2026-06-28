"""Audio post-production (kinora.md §9.6): pure filtergraph builders + a real
ffmpeg master pass that scores a bed, ducks it under narration, and loudness-
normalises. The ffmpeg-backed tests skip when no binary is available (mirroring
test_render_stitch.py); the filter-builder tests are pure and always run."""

from __future__ import annotations

import pytest

from app.providers.audio import (
    MasterPreset,
    MixProfile,
    SfxEvent,
    SfxKind,
    plan_mix,
)
from app.render import audio_post, degrade
from app.render.audio_post import (
    bed_filter,
    ducking_filter,
    loudnorm_filter,
    master_scene_audio,
    score_and_master,
    score_scene_to_audio,
)
from app.render.music import score_scene
from tests.test_render_support import wav_bytes

_CINE = MixProfile.for_preset(MasterPreset.CINEMATIC)


# --------------------------------------------------------------------------- #
# Pure filtergraph builders
# --------------------------------------------------------------------------- #


def test_loudnorm_filter_encodes_profile_target() -> None:
    f = loudnorm_filter(_CINE)
    assert "loudnorm=" in f
    assert "I=-16" in f
    assert "TP=-1.5" in f
    assert "LRA=11" in f


def test_ducking_filter_wires_sidechain() -> None:
    f = ducking_filter(_CINE, bed_label="bed", key_label="duckkey", out_label="ducked")
    assert "[bed][duckkey]sidechaincompress=" in f
    assert "[ducked]" in f
    assert f"ratio={_CINE.duck_ratio:.4f}".rstrip("0").rstrip(".") in f


def test_bed_filter_breathes_at_cue_tempo() -> None:
    plan = plan_mix(duration_s=8.0, cue=score_scene(mood_text="calm"), profile=_CINE)
    f = bed_filter(plan.bed)
    assert "amix=inputs=3" in f  # the three chord drones
    assert "tremolo=" in f  # the pad swell
    assert "afade=t=in" in f and "afade=t=out" in f  # head/tail fades
    assert "[bed]" in f


# --------------------------------------------------------------------------- #
# Real ffmpeg master pass
# --------------------------------------------------------------------------- #

pytestmark_ffmpeg = pytest.mark.skipif(
    not degrade.ffmpeg_available(), reason="no ffmpeg binary available"
)


@pytestmark_ffmpeg
def test_master_with_narration_ducks_and_loudnorms() -> None:
    plan = plan_mix(duration_s=3.0, cue=score_scene(mood_text="tense"), profile=_CINE)
    result = master_scene_audio(plan, narration_wav=wav_bytes(3.0))
    assert "bed" in result.applied
    assert "duck" in result.applied
    assert "loudnorm" in result.applied
    assert result.sample_rate == audio_post.MASTER_SAMPLE_RATE
    # A real, playable WAV with the expected length (mixed = longest of speech/bed).
    info = degrade.inspect(result.audio_bytes)
    assert info.has_audio is True
    assert abs(info.duration_s - 3.0) < 0.4


@pytestmark_ffmpeg
def test_master_bed_only_when_no_narration() -> None:
    plan = plan_mix(duration_s=2.5, cue=score_scene(mood_text="wondrous"), profile=_CINE)
    result = master_scene_audio(plan, narration_wav=None)
    assert "bed" in result.applied
    assert "duck" not in result.applied  # no speech to duck against
    assert abs(result.duration_s - 2.5) < 0.4


@pytestmark_ffmpeg
def test_master_with_sfx_overlay() -> None:
    sfx = [SfxEvent(at_s=0.5, kind=SfxKind.SPARKLE, duration_s=0.6)]
    plan = plan_mix(duration_s=3.0, cue=score_scene(mood_text="wondrous"), profile=_CINE, sfx=sfx)
    result = master_scene_audio(plan, narration_wav=wav_bytes(3.0))
    assert "sfx" in result.applied
    assert degrade.inspect(result.audio_bytes).has_audio is True


@pytestmark_ffmpeg
def test_master_narration_straight_when_no_music() -> None:
    # Zero-duration plan ⇒ no music; narration is mastered straight.
    plan = plan_mix(duration_s=0.0, cue=score_scene(mood_text="calm"), profile=_CINE)
    result = master_scene_audio(plan, narration_wav=wav_bytes(2.0))
    assert result.applied == ("loudnorm",)
    assert abs(result.duration_s - 2.0) < 0.4


@pytestmark_ffmpeg
def test_score_and_master_convenience() -> None:
    result = score_and_master(
        narration_wav=wav_bytes(2.0),
        cue=score_scene(mood_text="tender"),
        profile=MixProfile.for_preset(MasterPreset.DIALOGUE_FORWARD),
        duration_s=2.0,
    )
    info = degrade.inspect(result.audio_bytes)
    assert info.has_audio is True
    assert abs(info.duration_s - 2.0) < 0.4


def test_master_raises_when_nothing_to_mix() -> None:
    plan = plan_mix(duration_s=0.0, cue=score_scene(mood_text="calm"), profile=_CINE)
    with pytest.raises(ValueError, match="nothing to mix"):
        master_scene_audio(plan, narration_wav=None)


@pytestmark_ffmpeg
def test_score_scene_to_audio_full_entry_point() -> None:
    # One call from mood text → mastered track: tense scene gets a rumble SFX,
    # dialogue-forward master, and ducking under narration.
    result = score_scene_to_audio(
        narration_wav=wav_bytes(3.0),
        duration_s=3.0,
        mood_text="a tense, menacing confrontation",
        palette="cool",
    )
    assert "bed" in result.applied
    assert "sfx" in result.applied  # tense → default rumble accent
    assert "duck" in result.applied
    info = degrade.inspect(result.audio_bytes)
    assert info.has_audio is True
    assert abs(info.duration_s - 3.0) < 0.5
