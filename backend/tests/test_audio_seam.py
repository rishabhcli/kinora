"""Unit tests for the Generator drop-in seam + the TtsResult bridge + the
wav_duration helper. Deterministic — no network, no spend.

The load-bearing claim: a NarrationSeam over any UniversalAudioProvider matches
``app.providers.tts.TtsProvider.synthesize``'s signature and returns a real
``app.providers.types.TtsResult``, so the Generator can use it unchanged.
"""

from __future__ import annotations

import inspect
import io
import wave

import pytest

from app.audio.adapters import DescriptorAudioProvider, RawAudio
from app.audio.protocol import wav_duration
from app.audio.router import AudioRouter
from app.audio.seam import NarrationSeam, to_tts_result
from app.audio.types import (
    AlignmentMethod,
    AudioCapability,
    AudioResult,
    AudioTrackType,
    AudioWord,
    TtsRequest,
)
from app.providers.tts import TtsProvider
from app.providers.types import TtsResult


def make_wav(*, seconds: float = 1.0, sample_rate: int = 24000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(b"\x00\x00" * int(seconds * sample_rate))
    return buf.getvalue()


def _profile() -> AudioCapability:
    return AudioCapability(
        provider="fake",
        model="fake-1",
        track_types=frozenset({AudioTrackType.NARRATION}),
        supports_speed=True,
        supports_pitch=True,
    )


def _descriptor() -> DescriptorAudioProvider:
    async def synth(_req: TtsRequest) -> RawAudio:
        return RawAudio(
            audio_bytes=make_wav(seconds=1.0),
            word_timings=(("she", 0.0, 0.4), ("ran", 0.4, 0.9)),
        )

    return DescriptorAudioProvider(name="fake", profile=_profile(), synth=synth)


# --------------------------------------------------------------------------- #
# to_tts_result bridge
# --------------------------------------------------------------------------- #


def test_to_tts_result_exact_mapping() -> None:
    res = AudioResult(
        audio_bytes=b"abc",
        sample_rate=24000,
        duration_s=1.0,
        model="m",
        provider="p",
        word_timestamps=(AudioWord(text="hi", t_start=0.0, t_end=0.5),),
        alignment=AlignmentMethod.ASR,
    )
    tts = to_tts_result(res)
    assert isinstance(tts, TtsResult)
    assert tts.audio_bytes == b"abc"
    assert tts.sample_rate == 24000
    assert tts.alignment == "asr"
    assert [w.text for w in tts.word_timestamps] == ["hi"]


def test_to_tts_result_none_alignment_maps_to_proportional() -> None:
    res = AudioResult(
        audio_bytes=b"x", sample_rate=24000, duration_s=1.0, model="m", provider="p",
        alignment=AlignmentMethod.NONE,
    )
    assert to_tts_result(res).alignment == "proportional"


# --------------------------------------------------------------------------- #
# NarrationSeam — Generator drop-in
# --------------------------------------------------------------------------- #


def test_seam_signature_matches_tts_provider() -> None:
    """The seam's synthesize signature is a superset-compatible match for the
    provider's, so the Generator call site (text, voice_id=...) is unchanged."""
    seam_params = set(inspect.signature(NarrationSeam.synthesize).parameters)
    prov_params = set(inspect.signature(TtsProvider.synthesize).parameters)
    assert prov_params == seam_params


@pytest.mark.asyncio
async def test_seam_returns_real_tts_result() -> None:
    seam = NarrationSeam(_descriptor())
    out = await seam.synthesize("she ran", voice_id="Cherry")
    assert isinstance(out, TtsResult)
    assert [w.text for w in out.word_timestamps] == ["she", "ran"]
    assert out.alignment == "model"
    assert out.audio_format == "wav"


@pytest.mark.asyncio
async def test_seam_over_router() -> None:
    seam = NarrationSeam(AudioRouter([_descriptor()]))
    out = await seam.synthesize("she ran", voice_id="Cherry", speed=1.1, pitch=0.9)
    assert isinstance(out, TtsResult)
    assert out.duration_s == pytest.approx(1.0, abs=0.05)


@pytest.mark.asyncio
async def test_seam_passes_word_timestamps_flag() -> None:
    captured: dict[str, object] = {}

    async def synth(req: TtsRequest) -> RawAudio:
        captured["word_timestamps"] = req.word_timestamps
        captured["language"] = req.language
        return RawAudio(audio_bytes=make_wav())

    seam = NarrationSeam(DescriptorAudioProvider(name="f", profile=_profile(), synth=synth))
    await seam.synthesize("x", voice_id="Cherry", word_timestamps=False, language_type="Spanish")
    assert captured["word_timestamps"] is False
    assert captured["language"] == "Spanish"


# --------------------------------------------------------------------------- #
# wav_duration helper
# --------------------------------------------------------------------------- #


def test_wav_duration_real_wav() -> None:
    raw = make_wav(seconds=2.0, sample_rate=16000)
    dur, sr = wav_duration(raw, 24000)
    assert dur == pytest.approx(2.0, abs=0.01)
    assert sr == 16000


def test_wav_duration_garbage_falls_back() -> None:
    dur, sr = wav_duration(b"not a wav at all" * 100, 24000)
    assert dur >= 0.0
    assert sr == 24000
