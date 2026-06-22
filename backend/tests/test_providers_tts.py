"""Unit tests for the TTS provider: proportional alignment, WAV duration math,
ASR word extraction, synthesize (proportional fallback), and voice cloning."""

from __future__ import annotations

import io
import wave

import httpx
import pytest

from app.providers.tts import (
    TtsProvider,
    _extract_asr_words,
    _wav_duration,
    proportional_alignment,
    resolve_tts_model,
)
from tests.test_providers_base import make_client


def _wav(seconds: float, rate: int = 24000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(rate * seconds))
    return buf.getvalue()


WAV_1S = _wav(1.0)


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #


def test_resolve_tts_model_maps_family_to_snapshot() -> None:
    assert resolve_tts_model("qwen3-tts-vc") == "qwen3-tts-vc-2026-01-22"
    assert resolve_tts_model("some-exact-id") == "some-exact-id"


def test_proportional_alignment_covers_duration_monotonically() -> None:
    words = proportional_alignment("Red fox runs fast", 4.0)
    assert [w.text for w in words] == ["Red", "fox", "runs", "fast"]
    assert words[0].t_start == 0.0
    assert words[-1].t_end <= 4.0
    for word in words:
        assert word.t_end > word.t_start
    for earlier, later in zip(words, words[1:], strict=False):
        assert later.t_start >= earlier.t_start


def test_proportional_alignment_empty_text() -> None:
    assert proportional_alignment("", 3.0) == []
    assert proportional_alignment("hi", 0.0) == []


def test_wav_duration_reads_valid_header() -> None:
    duration, rate = _wav_duration(WAV_1S, 24000)
    assert rate == 24000
    assert abs(duration - 1.0) < 0.05


def test_wav_duration_falls_back_on_unparseable() -> None:
    # Not a RIFF stream -> wave.open fails -> byte-math fallback (16-bit mono):
    # (24044 - 44 header) / 2 bytes-per-sample / 24000 Hz = 0.5s.
    duration, rate = _wav_duration(b"\x00" * 24044, 24000)
    assert rate == 24000
    assert abs(duration - 0.5) < 0.02


def test_extract_asr_words_converts_ms() -> None:
    words = _extract_asr_words(
        {
            "words": [
                {"text": "red", "begin_time": 0, "end_time": 300},
                {"text": "fox", "begin_time": 300, "end_time": 650},
            ]
        }
    )
    assert [w.text for w in words] == ["red", "fox"]
    assert words[1].t_start == 0.3
    assert words[1].t_end == 0.65


# --------------------------------------------------------------------------- #
# synthesize + clone (transport-mocked)
# --------------------------------------------------------------------------- #


def _tts_handler(request: httpx.Request) -> httpx.Response:
    if request.url.host == "assets.test":
        return httpx.Response(200, content=WAV_1S)
    if request.url.path.endswith("/services/audio/tts/customization"):
        return httpx.Response(
            200, json={"output": {"voice": "voice-abc123"}, "request_id": "enr-1"}
        )
    return httpx.Response(200, json={})


class _FakeTtsResp:
    def __init__(self, url: str) -> None:
        self.status_code = 200
        self.code = None
        self.message = None
        self.request_id = "tts-req"
        self.output = {"audio": {"url": url}}


async def test_synthesize_returns_audio_and_proportional_timestamps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import dashscope
    import dashscope.audio.qwen_asr as qa

    monkeypatch.setattr(
        dashscope.MultiModalConversation,
        "call",
        lambda **kw: _FakeTtsResp("https://assets.test/narration.wav"),
    )

    # Force the ASR aligner to be unavailable so we exercise the fallback path.
    def _no_asr(**kwargs: object) -> object:
        raise RuntimeError("asr unavailable in test")

    monkeypatch.setattr(qa.QwenTranscription, "call", _no_asr)

    client = make_client(_tts_handler)
    result = await TtsProvider(client).synthesize("The fox ran fast.", voice_id="Cherry")
    assert result.audio_bytes == WAV_1S
    assert result.sample_rate == 24000
    assert result.alignment == "proportional"
    assert len(result.word_timestamps) == 4
    assert result.model == "qwen3-tts-flash"
    totals = client.usage_totals
    assert totals is not None and totals.audio_seconds > 0
    await client.aclose()


async def test_synthesize_uses_asr_alignment_when_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import dashscope
    import dashscope.audio.qwen_asr as qa

    monkeypatch.setattr(
        dashscope.MultiModalConversation,
        "call",
        lambda **kw: _FakeTtsResp("https://assets.test/narration.wav"),
    )

    class _AsrResp:
        status_code = 200
        code = None
        message = None
        request_id = "asr-1"
        output = {
            "words": [
                {"text": "Red", "begin_time": 0, "end_time": 500},
                {"text": "fox", "begin_time": 500, "end_time": 980},
            ]
        }

    monkeypatch.setattr(qa.QwenTranscription, "call", lambda **kw: _AsrResp())
    client = make_client(_tts_handler)
    result = await TtsProvider(client).synthesize("Red fox", voice_id="Cherry")
    assert result.alignment == "asr"
    assert [w.text for w in result.word_timestamps] == ["Red", "fox"]
    await client.aclose()


async def test_clone_voice_returns_voice_id() -> None:
    client = make_client(_tts_handler)
    voice_id = await TtsProvider(client).clone_voice(b"RIFFfake-wav-bytes")
    assert voice_id == "voice-abc123"
    await client.aclose()
