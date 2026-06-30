"""Unit tests for the canonical audio vocabulary: capability matching, request
limits, the music-bed spec, and result word-dict export. Pure data, no spend."""

from __future__ import annotations

from app.audio.types import (
    AlignmentMethod,
    AudioCapability,
    AudioFormat,
    AudioResult,
    AudioTrackType,
    AudioWord,
    MusicBedSpec,
    TtsRequest,
)


def _cap(**kw: object) -> AudioCapability:
    base: dict[str, object] = {"provider": "p", "model": "m"}
    base.update(kw)
    return AudioCapability(**base)


def test_capability_voice_matching() -> None:
    cap = _cap(voices=("Cherry", "Ryan"))
    assert cap.supports_voice("Cherry")
    assert cap.supports_voice(None)  # default allowed
    assert not cap.supports_voice("Unknown")


def test_capability_open_voice_set_accepts_any() -> None:
    cap = _cap(voices=())  # no enumerated presets -> open
    assert cap.supports_voice("anything")


def test_capability_cloning_accepts_non_preset_voice() -> None:
    cap = _cap(voices=("Cherry",), supports_voice_cloning=True)
    assert cap.supports_voice("clone-xyz-123")


def test_capability_language_and_track() -> None:
    cap = _cap(languages=("en-US",), track_types=frozenset({AudioTrackType.NARRATION}))
    assert cap.supports_language("en-US")
    assert not cap.supports_language("fr-FR")
    assert cap.supports_language(None)
    assert cap.supports_track(AudioTrackType.NARRATION)
    assert not cap.supports_track(AudioTrackType.MUSIC)


def test_capability_defaults() -> None:
    cap = _cap(sample_rates=(48000, 24000), formats=(AudioFormat.MP3, AudioFormat.WAV))
    assert cap.default_sample_rate() == 48000
    assert cap.default_format() is AudioFormat.MP3


def test_request_char_count() -> None:
    assert TtsRequest(text="hello").char_count() == 5


def test_music_bed_to_request() -> None:
    bed = MusicBedSpec(prompt="soft strings", track_type=AudioTrackType.MUSIC, bed_gain=0.15)
    req = bed.to_request()
    assert req.text == "soft strings"
    assert req.track_type is AudioTrackType.MUSIC
    assert req.word_timestamps is False
    assert req.volume == 0.15


def test_result_words_as_dicts() -> None:
    res = AudioResult(
        audio_bytes=b"x",
        sample_rate=24000,
        duration_s=1.0,
        model="m",
        provider="p",
        word_timestamps=(AudioWord(text="hi", t_start=0.0, t_end=0.5),),
        alignment=AlignmentMethod.MODEL,
    )
    assert res.words_as_dicts() == [{"text": "hi", "t_start": 0.0, "t_end": 0.5}]


def test_audioword_duration_never_negative() -> None:
    assert AudioWord(text="x", t_start=0.5, t_end=0.4).duration == 0.0
    assert AudioWord(text="x", t_start=0.1, t_end=0.6).duration == 0.5
