"""Unit tests for the descriptor-driven adapter + the hosted-engine adapters +
the DashScope wrapper. Deterministic fakes only — no network, no key, no spend.

The single network seam (each adapter's synth callable) is replaced by a pure
fake that returns a tiny real WAV, so duration measurement, capability gating,
and the alignment FALLBACK are all exercised offline.
"""

from __future__ import annotations

import io
import wave

import pytest

from app.audio.adapters import (
    AzureAudioAdapter,
    DashScopeAudioAdapter,
    DescriptorAudioProvider,
    ElevenLabsAudioAdapter,
    GoogleAudioAdapter,
    OpenAiAudioAdapter,
    RawAudio,
    azure_profile,
)
from app.audio.types import (
    AlignmentMethod,
    AudioCapability,
    AudioFormat,
    AudioTrackType,
    TtsRequest,
)


def make_wav(*, seconds: float = 1.0, sample_rate: int = 24000) -> bytes:
    """A real, parseable mono 16-bit WAV of the given length (silence)."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(b"\x00\x00" * int(seconds * sample_rate))
    return buf.getvalue()


# --------------------------------------------------------------------------- #
# Descriptor adapter
# --------------------------------------------------------------------------- #


def _profile(**kw: object) -> AudioCapability:
    base: dict[str, object] = {
        "provider": "fake",
        "model": "fake-1",
        "track_types": frozenset({AudioTrackType.NARRATION}),
    }
    base.update(kw)
    return AudioCapability(**base)


@pytest.mark.asyncio
async def test_descriptor_uses_inline_model_timings() -> None:
    async def synth(_req: TtsRequest) -> RawAudio:
        return RawAudio(
            audio_bytes=make_wav(seconds=1.0),
            word_timings=(("she", 0.0, 0.4), ("ran", 0.4, 0.9)),
        )

    p = DescriptorAudioProvider(name="x", profile=_profile(), synth=synth)
    res = await p.synthesize(TtsRequest(text="she ran"))
    assert res.alignment is AlignmentMethod.MODEL
    assert [w.text for w in res.word_timestamps] == ["she", "ran"]
    assert res.duration_s == pytest.approx(1.0, abs=0.05)
    assert res.provider == "fake"


@pytest.mark.asyncio
async def test_descriptor_falls_back_when_no_timings() -> None:
    async def synth(_req: TtsRequest) -> RawAudio:
        return RawAudio(audio_bytes=make_wav(seconds=2.0))  # no word_timings

    p = DescriptorAudioProvider(name="x", profile=_profile(), synth=synth)
    res = await p.synthesize(TtsRequest(text="she ran far away"))
    assert res.alignment is AlignmentMethod.PROPORTIONAL
    assert len(res.word_timestamps) == 4
    # Anchored to the measured 2s clip.
    assert res.word_timestamps[-1].t_end <= 2.05


@pytest.mark.asyncio
async def test_descriptor_asr_provenance() -> None:
    async def synth(_req: TtsRequest) -> RawAudio:
        return RawAudio(
            audio_bytes=make_wav(),
            word_timings=(("hi", 0.0, 0.5),),
            from_asr=True,
        )

    p = DescriptorAudioProvider(name="x", profile=_profile(), synth=synth)
    res = await p.synthesize(TtsRequest(text="hi"))
    assert res.alignment is AlignmentMethod.ASR


@pytest.mark.asyncio
async def test_descriptor_normalizes_ms_timings() -> None:
    async def synth(_req: TtsRequest) -> RawAudio:
        return RawAudio(audio_bytes=make_wav(), word_timings=(("hi", 100.0, 500.0),))

    p = DescriptorAudioProvider(name="x", profile=_profile(), synth=synth)
    res = await p.synthesize(TtsRequest(text="hi"))
    assert res.word_timestamps[0].t_end == pytest.approx(0.5)


@pytest.mark.asyncio
async def test_descriptor_no_timing_for_music_track() -> None:
    async def synth(_req: TtsRequest) -> RawAudio:
        return RawAudio(audio_bytes=make_wav(), audio_format=AudioFormat.MP3)

    prof = _profile(track_types=frozenset({AudioTrackType.MUSIC}))
    p = DescriptorAudioProvider(name="x", profile=prof, synth=synth)
    res = await p.synthesize(TtsRequest(text="strings", track_type=AudioTrackType.MUSIC))
    assert res.word_timestamps == ()
    assert res.alignment is AlignmentMethod.NONE
    assert res.track_type is AudioTrackType.MUSIC


@pytest.mark.asyncio
async def test_descriptor_validation_rejects_bad_track() -> None:
    async def synth(_req: TtsRequest) -> RawAudio:  # pragma: no cover - never called
        return RawAudio(audio_bytes=make_wav())

    p = DescriptorAudioProvider(name="x", profile=_profile(), synth=synth)
    with pytest.raises(ValueError, match="track"):
        await p.synthesize(TtsRequest(text="x", track_type=AudioTrackType.MUSIC))


@pytest.mark.asyncio
async def test_descriptor_validation_rejects_over_char_limit() -> None:
    async def synth(_req: TtsRequest) -> RawAudio:  # pragma: no cover
        return RawAudio(audio_bytes=make_wav())

    p = DescriptorAudioProvider(name="x", profile=_profile(max_input_chars=3), synth=synth)
    with pytest.raises(ValueError, match="limit"):
        await p.synthesize(TtsRequest(text="toolong"))


@pytest.mark.asyncio
async def test_descriptor_rejects_ssml_when_unsupported() -> None:
    async def synth(_req: TtsRequest) -> RawAudio:  # pragma: no cover
        return RawAudio(audio_bytes=make_wav())

    p = DescriptorAudioProvider(name="x", profile=_profile(supports_ssml=False), synth=synth)
    with pytest.raises(ValueError, match="SSML"):
        await p.synthesize(TtsRequest(text="<speak>x</speak>", is_ssml=True))


@pytest.mark.asyncio
async def test_descriptor_health_probe() -> None:
    calls = {"n": 0}

    async def probe() -> bool:
        calls["n"] += 1
        return False

    async def synth(_req: TtsRequest) -> RawAudio:  # pragma: no cover
        return RawAudio(audio_bytes=make_wav())

    p = DescriptorAudioProvider(name="x", profile=_profile(), synth=synth, health_probe=probe)
    assert await p.healthy() is False
    assert calls["n"] == 1


# --------------------------------------------------------------------------- #
# Hosted adapters (profiles + the descriptor substrate)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_elevenlabs_adapter_uses_fallback() -> None:
    async def synth(_req: TtsRequest) -> RawAudio:
        return RawAudio(audio_bytes=make_wav(seconds=1.5), audio_format=AudioFormat.MP3)

    p = ElevenLabsAudioAdapter(synth)
    assert p.capabilities().provider == "elevenlabs"
    assert p.capabilities().supports_word_timestamps is False
    res = await p.synthesize(TtsRequest(text="hello there world"))
    assert res.alignment is AlignmentMethod.PROPORTIONAL
    assert res.audio_format is AudioFormat.MP3


@pytest.mark.asyncio
async def test_openai_adapter_voice_gate() -> None:
    async def synth(_req: TtsRequest) -> RawAudio:  # pragma: no cover
        return RawAudio(audio_bytes=make_wav())

    p = OpenAiAudioAdapter(synth)
    assert "nova" in p.capabilities().voices
    with pytest.raises(ValueError, match="voice"):
        await p.synthesize(TtsRequest(text="x", voice_id="not-a-real-voice"))


@pytest.mark.asyncio
async def test_azure_adapter_supports_inline_timestamps() -> None:
    async def synth(_req: TtsRequest) -> RawAudio:
        return RawAudio(audio_bytes=make_wav(), word_timings=(("hi", 0.0, 500.0),))

    p = AzureAudioAdapter(synth)
    cap = p.capabilities()
    assert cap.supports_word_timestamps is True
    assert cap.supports_ssml is True
    res = await p.synthesize(TtsRequest(text="hi", voice_id="en-US-JennyNeural"))
    assert res.alignment is AlignmentMethod.MODEL


@pytest.mark.asyncio
async def test_google_adapter_profile() -> None:
    async def synth(_req: TtsRequest) -> RawAudio:
        return RawAudio(audio_bytes=make_wav())

    p = GoogleAudioAdapter(synth)
    assert p.capabilities().provider == "google"
    assert "hi-IN" in p.capabilities().languages
    res = await p.synthesize(TtsRequest(text="hello", voice_id="en-US-Neural2-A"))
    assert res.provider == "google"


def test_azure_profile_factory_is_frozen() -> None:
    prof = azure_profile()
    assert prof.provider == "azure"
    assert prof.supports_emotion is True


# --------------------------------------------------------------------------- #
# DashScope wrapper (preserves the existing provider's behaviour + provenance)
# --------------------------------------------------------------------------- #


class _FakeTtsWord:
    def __init__(self, text: str, t0: float, t1: float) -> None:
        self.text, self.t_start, self.t_end = text, t0, t1


class _FakeTtsResult:
    def __init__(self, *, alignment: str, words: list[_FakeTtsWord]) -> None:
        self.audio_bytes = make_wav(seconds=1.2)
        self.sample_rate = 24000
        self.duration_s = 1.2
        self.word_timestamps = words
        self.alignment = alignment
        self.voice_id: str | None = "Cherry"
        self.model = "qwen3-tts-flash"
        self.audio_format = "wav"


class _FakeTtsProvider:
    """Stands in for app.providers.tts.TtsProvider (the slice the adapter calls)."""

    def __init__(self, result: _FakeTtsResult) -> None:
        self._result = result
        self.last_kwargs: dict[str, object] = {}

    async def synthesize(
        self,
        text: str,
        *,
        voice_id: str,
        speed: float = 1.0,
        pitch: float = 1.0,
        word_timestamps: bool = True,
        model: str | None = None,
        language_type: str = "English",
        timeout: float | None = None,
    ) -> _FakeTtsResult:
        self.last_kwargs = {
            "text": text,
            "voice_id": voice_id,
            "speed": speed,
            "pitch": pitch,
            "word_timestamps": word_timestamps,
            "model": model,
            "language_type": language_type,
            "timeout": timeout,
        }
        return self._result


@pytest.mark.asyncio
async def test_dashscope_adapter_preserves_asr_provenance() -> None:
    inner = _FakeTtsResult(
        alignment="asr", words=[_FakeTtsWord("she", 0.0, 0.5), _FakeTtsWord("ran", 0.5, 1.1)]
    )
    provider = _FakeTtsProvider(inner)
    adapter = DashScopeAudioAdapter(provider)
    res = await adapter.synthesize(TtsRequest(text="she ran", voice_id="Cherry"))
    assert res.provider == "dashscope"
    assert res.alignment is AlignmentMethod.ASR
    assert [w.text for w in res.word_timestamps] == ["she", "ran"]
    # The canonical request mapped onto the provider's kwargs.
    assert provider.last_kwargs["voice_id"] == "Cherry"
    assert provider.last_kwargs["language_type"] == "English"


@pytest.mark.asyncio
async def test_dashscope_adapter_proportional_provenance() -> None:
    inner = _FakeTtsResult(
        alignment="proportional",
        words=[_FakeTtsWord("she", 0.0, 0.6), _FakeTtsWord("ran", 0.6, 1.2)],
    )
    adapter = DashScopeAudioAdapter(_FakeTtsProvider(inner))
    res = await adapter.synthesize(TtsRequest(text="she ran"))
    assert res.alignment is AlignmentMethod.PROPORTIONAL


@pytest.mark.asyncio
async def test_dashscope_adapter_default_voice() -> None:
    inner = _FakeTtsResult(alignment="asr", words=[_FakeTtsWord("x", 0.0, 0.5)])
    provider = _FakeTtsProvider(inner)
    adapter = DashScopeAudioAdapter(provider, default_voice="Ryan")
    await adapter.synthesize(TtsRequest(text="x"))  # no voice_id -> default
    assert provider.last_kwargs["voice_id"] == "Ryan"


@pytest.mark.asyncio
async def test_dashscope_adapter_rejects_non_narration() -> None:
    adapter = DashScopeAudioAdapter(_FakeTtsProvider(
        _FakeTtsResult(alignment="asr", words=[])
    ))
    with pytest.raises(ValueError, match="narration"):
        await adapter.synthesize(TtsRequest(text="x", track_type=AudioTrackType.MUSIC))


@pytest.mark.asyncio
async def test_dashscope_capability_profile() -> None:
    adapter = DashScopeAudioAdapter(_FakeTtsProvider(
        _FakeTtsResult(alignment="asr", words=[])
    ))
    cap = adapter.capabilities()
    assert cap.provider == "dashscope"
    assert "Cherry" in cap.voices
    assert cap.supports_voice_cloning is True
