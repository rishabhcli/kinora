"""Unit tests for the audio mix planner: profile presets, intensity→gain mapping,
SFX clamping/sorting, and the MusicProvider seam. Pure — no ffmpeg, no audio."""

from __future__ import annotations

from app.providers.audio import (
    LocalCueLibrary,
    MasterPreset,
    MixProfile,
    MusicProvider,
    SfxEvent,
    SfxKind,
    default_scene_sfx,
    plan_mix,
    recommend_profile,
)
from app.render.music import Mood, cue_for_mood, score_scene


def test_profile_for_preset_returns_canonical() -> None:
    for preset in MasterPreset:
        profile = MixProfile.for_preset(preset)
        assert profile.preset is preset
        assert profile.target_lufs < 0  # LUFS is always negative
        assert profile.duck_ratio >= 1.0


def test_dialogue_forward_ducks_harder_and_quieter_bed() -> None:
    cine = MixProfile.for_preset(MasterPreset.CINEMATIC)
    dlg = MixProfile.for_preset(MasterPreset.DIALOGUE_FORWARD)
    assert dlg.duck_ratio > cine.duck_ratio
    assert dlg.music_gain_db < cine.music_gain_db  # quieter bed


def test_local_cue_library_maps_intensity_to_gain() -> None:
    lib = LocalCueLibrary()
    loud = lib.resolve_bed(cue_for_mood(Mood.TRIUMPHANT), duration_s=10.0)
    quiet = lib.resolve_bed(cue_for_mood(Mood.CALM), duration_s=10.0)
    assert loud.gain_db > quiet.gain_db  # higher intensity → louder bed
    assert lib.min_gain_db <= quiet.gain_db <= lib.max_gain_db


def test_local_cue_library_satisfies_music_provider() -> None:
    assert isinstance(LocalCueLibrary(), MusicProvider)


def test_plan_mix_folds_profile_gain_onto_bed() -> None:
    cue = score_scene(mood_text="tense")
    plan = plan_mix(
        duration_s=12.0,
        cue=cue,
        profile=MixProfile.for_preset(MasterPreset.DIALOGUE_FORWARD),
    )
    bare = LocalCueLibrary().resolve_bed(cue, duration_s=12.0).gain_db
    assert plan.bed.gain_db == round(bare + plan.profile.music_gain_db, 2)
    assert plan.duration_s == 12.0
    assert plan.has_music is True


def test_plan_mix_clamps_and_sorts_sfx() -> None:
    cue = score_scene(mood_text="wondrous")
    sfx = [
        SfxEvent(at_s=8.0, kind=SfxKind.SPARKLE),
        SfxEvent(at_s=-2.0, kind=SfxKind.CHIME),  # negative → clamps to 0
        SfxEvent(at_s=100.0, kind=SfxKind.IMPACT),  # past end → dropped
        SfxEvent(at_s=3.0, kind=SfxKind.WHOOSH),
    ]
    plan = plan_mix(
        duration_s=10.0,
        cue=cue,
        profile=MixProfile.for_preset(MasterPreset.CINEMATIC),
        sfx=sfx,
    )
    times = [e.at_s for e in plan.sfx]
    assert times == sorted(times)  # sorted
    assert 0.0 in times  # the negative one clamped to 0
    assert all(t < 10.0 for t in times)  # past-end dropped
    assert len(plan.sfx) == 3


def test_plan_mix_fades_never_exceed_half_duration() -> None:
    plan = plan_mix(
        duration_s=1.0,
        cue=score_scene(mood_text="calm"),
        profile=MixProfile.for_preset(MasterPreset.CINEMATIC),
    )
    assert plan.bed.fade_in_s <= 0.5
    assert plan.bed.fade_out_s <= 0.5


def test_plan_mix_zero_duration_has_no_music() -> None:
    plan = plan_mix(
        duration_s=0.0,
        cue=score_scene(mood_text="tense"),
        profile=MixProfile.for_preset(MasterPreset.CINEMATIC),
    )
    assert plan.has_music is False
    assert plan.sfx == ()


def test_default_scene_sfx_per_mood() -> None:
    assert default_scene_sfx("tense", duration_s=10.0)[0].kind is SfxKind.RUMBLE
    assert default_scene_sfx("wondrous", duration_s=10.0)[0].kind is SfxKind.SPARKLE
    assert default_scene_sfx("calm", duration_s=10.0) == []  # calm needs no accent
    assert default_scene_sfx("tense", duration_s=0.0) == []  # no time → none


def test_plan_mix_custom_music_provider() -> None:
    class LoudLibrary:
        def resolve_bed(self, cue, *, duration_s):  # type: ignore[no-untyped-def]
            from app.providers.audio import MusicBedSpec

            return MusicBedSpec(cue=cue, duration_s=duration_s, gain_db=0.0)

    plan = plan_mix(
        duration_s=5.0,
        cue=score_scene(mood_text="calm"),
        profile=MixProfile.for_preset(MasterPreset.CINEMATIC),
        music=LoudLibrary(),
    )
    # 0 dB bed + cinematic music_gain (-6) folded on.
    assert plan.bed.gain_db == round(0.0 + plan.profile.music_gain_db, 2)


def test_recommend_profile_per_mood() -> None:
    # Tense / sombre → speech-forward; triumphant → punchy; calm → quiet room.
    assert recommend_profile(Mood.TENSE).preset is MasterPreset.DIALOGUE_FORWARD
    assert recommend_profile(Mood.SOMBRE).preset is MasterPreset.DIALOGUE_FORWARD
    assert recommend_profile(Mood.TRIUMPHANT).preset is MasterPreset.PUNCHY
    assert recommend_profile(Mood.CALM).preset is MasterPreset.QUIET_ROOM
    # A mood with no strong preference → the balanced default.
    assert recommend_profile(Mood.NEUTRAL).preset is MasterPreset.CINEMATIC
    assert recommend_profile(Mood.WONDROUS).preset is MasterPreset.CINEMATIC
