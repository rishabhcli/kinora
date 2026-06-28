"""Narration: voice cloning + TTS with the word-timing map (§9.4).

DashScope's available TTS family on the intl tier is **Qwen3-TTS** (incl. the
``qwen3-tts-vc`` voice-clone model). It returns a real WAV but, unlike CosyVoice,
does **not** emit word timestamps inline (Alibaba's guidance is to forced-align
after synthesis). So word timing — which drives karaoke + page-turn — is produced
by a real aligner:

1. ``asr``: DashScope ASR (``qwen3-asr-flash``) forced-alignment over the
   synthesized waveform. Used when the audio is reachable at a plain URL (e.g.
   once persisted to object storage in a later phase).
2. ``proportional``: distribute the *measured* audio duration across words
   weighted by token length. Anchored to the real waveform length; used when an
   ASR-reachable URL isn't available.

``TtsResult.alignment`` always records which path produced the timing.
"""

from __future__ import annotations

import contextlib
import functools
import io
import re
import wave
from typing import Any

from app.core.logging import get_logger

from .base import ProviderClient, data_uri
from .base import sdk_get as _get
from .errors import ProviderBadRequest, ProviderError, ResponseParseError
from .prosody import ProsodyPlan, plan_prosody
from .types import TtsResult, TtsWord, Usage

logger = get_logger("app.providers.tts")

#: A known-good hosted preset voice used when an assigned per-character voice is
#: rejected by the model snapshot (the supported set varies by revision). Falling
#: back keeps narration playing instead of discarding an already-rendered shot.
_FALLBACK_VOICE = "Cherry"

#: Map bare model families (as configured) to the concrete intl snapshot ids.
#: The bare ``qwen3-tts-flash`` alias returns 403 AllocationQuota.FreeTierOnly on
#: the intl free tier (the alias tracks a newer paid-only revision), but the dated
#: ``qwen3-tts-flash-2025-09-18`` snapshot retains free quota — so pin to it.
#: Verify with scripts/provider_preflight.py --spend-smoke before changing.
_TTS_MODEL_SNAPSHOTS = {
    "qwen3-tts-vc": "qwen3-tts-vc-2026-01-22",
    "qwen3-tts-flash": "qwen3-tts-flash-2025-09-18",
    "qwen3-tts-instruct-flash": "qwen3-tts-instruct-flash",
}
_DEFAULT_SAMPLE_RATE = 24000
_ASR_MODEL = "qwen3-asr-flash"
_WORD_RE = re.compile(r"\S+")


def resolve_tts_model(model: str) -> str:
    """Resolve a configured family alias to a concrete synthesizable model id."""
    return _TTS_MODEL_SNAPSHOTS.get(model, model)


def _wav_duration(raw: bytes, default_sr: int) -> tuple[float, int]:
    """Return ``(duration_s, sample_rate)``, robust to streaming WAV headers.

    Streaming TTS WAVs ship a placeholder size in the header (a bogus frame
    count), so we cross-check the declared frame count against the actual byte
    length and trust whichever is consistent.
    """
    try:
        with contextlib.closing(wave.open(io.BytesIO(raw))) as wf:
            sr = wf.getframerate() or default_sr
            frame_bytes = max(wf.getsampwidth() * wf.getnchannels(), 1)
            frames_from_bytes = max(len(raw) - 44, 0) / frame_bytes
            declared = wf.getnframes()
            frames = declared if 0 < declared <= frames_from_bytes * 4 else frames_from_bytes
            return max(frames / sr, 0.0), sr
    except (wave.Error, EOFError, OSError):
        return max((len(raw) - 44) / 2 / default_sr, 0.0), default_sr


def proportional_alignment(
    text: str,
    duration_s: float,
    *,
    gap_ratio: float = 0.06,
) -> list[TtsWord]:
    """Distribute ``duration_s`` across the words of ``text`` by token length.

    Real measured total duration; per-word split weighted by characters with a
    small inter-word gap so highlights don't visually run together.
    """
    tokens = _WORD_RE.findall(text)
    if not tokens or duration_s <= 0:
        return []
    weights = [len(tok) + 1 for tok in tokens]
    total_weight = sum(weights)
    speech = duration_s * (1.0 - gap_ratio)
    gap = (duration_s * gap_ratio) / max(len(tokens), 1)
    words: list[TtsWord] = []
    cursor = 0.0
    for tok, weight in zip(tokens, weights, strict=True):
        span = speech * (weight / total_weight)
        start = cursor
        end = min(duration_s, start + span)
        words.append(TtsWord(text=tok, t_start=round(start, 3), t_end=round(end, 3)))
        cursor = end + gap
    return words


def _extract_asr_words(node: Any) -> list[TtsWord]:
    """Best-effort recursive scrape of word-level timings from an ASR response."""
    found: list[TtsWord] = []

    def visit(obj: Any) -> None:
        if isinstance(obj, dict):
            text = obj.get("text") or obj.get("word")
            start = obj.get("begin_time", obj.get("start_time", obj.get("start")))
            end = obj.get("end_time", obj.get("end_time", obj.get("end")))
            if text is not None and start is not None and end is not None:
                t0, t1 = float(start), float(end)
                # DashScope ASR reports ms; convert when the magnitude looks like ms.
                if t1 > 100:
                    t0, t1 = t0 / 1000.0, t1 / 1000.0
                found.append(TtsWord(text=str(text), t_start=round(t0, 3), t_end=round(t1, 3)))
            for value in obj.values():
                visit(value)
        elif isinstance(obj, list):
            for item in obj:
                visit(item)

    visit(node)
    return found


class TtsProvider:
    """Async voice-clone + narration client."""

    def __init__(self, client: ProviderClient) -> None:
        self._client = client
        self._settings = client.settings

    async def clone_voice(
        self,
        reference_audio: bytes,
        *,
        audio_mime: str = "audio/wav",
        preferred_name: str = "kinora",
        target_model: str | None = None,
        timeout: float | None = None,
    ) -> str:
        """Enroll a cloned voice from a short reference clip; returns its voice id.

        Uses the native ``qwen-voice-enrollment`` customization endpoint, which
        accepts inline base64 audio (no separate upload required).
        """
        target = resolve_tts_model(target_model or self._settings.tts_clone_model)
        payload = {
            "model": "qwen-voice-enrollment",
            "input": {
                "action": "create",
                "target_model": target,
                "preferred_name": preferred_name,
                "audio": {"data": data_uri(reference_audio, audio_mime)},
            },
        }
        body = await self._client.request_json(
            "POST",
            f"{self._client.native_base}/services/audio/tts/customization",
            op="tts_clone",
            model=target,
            json=payload,
            timeout=timeout,
        )
        output = body.get("output") or {}
        request_id = body.get("request_id")
        voice_id = output.get("voice") or output.get("voice_id")
        if not voice_id:
            raise ResponseParseError("voice enrollment returned no voice id", request_id=request_id)
        self._client.record_usage(Usage(model=target, operation="tts", request_id=request_id))
        return str(voice_id)

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
    ) -> TtsResult:
        """Synthesize narration and build the word-timing map.

        ``speed``/``pitch`` are accepted for interface stability; Qwen3-TTS does
        not expose prosody knobs (instruction control is via the instruct model),
        so they are recorded but not forced onto the request.
        """
        from dashscope import MultiModalConversation

        model = resolve_tts_model(model or self._settings.tts_model)

        async def _synth(voice: str) -> Any:
            call = functools.partial(
                MultiModalConversation.call,
                api_key=self._client.api_key,
                model=model,
                text=text,
                voice=voice,
                language_type=language_type,
                stream=False,
            )
            return await self._client.call_sdk(call, op="tts", model=model, timeout=timeout)

        used_voice = voice_id
        try:
            rsp = await _synth(used_voice)
        except ProviderBadRequest as exc:
            # A per-character preset voice can be unsupported by the hosted
            # snapshot (the supported set varies by model revision). Never let an
            # unsupported voice discard an already-rendered shot — retry once with
            # a known-good voice so narration still plays.
            if used_voice == _FALLBACK_VOICE or "voice" not in str(exc).lower():
                raise
            logger.warning(
                "tts.voice_fallback",
                requested=used_voice,
                fallback=_FALLBACK_VOICE,
                error=str(exc),
            )
            used_voice = _FALLBACK_VOICE
            rsp = await _synth(used_voice)
        audio_node = _get(_get(rsp, "output"), "audio")
        url = _get(audio_node, "url")
        if not url:
            raise ResponseParseError("TTS response contained no audio url")
        audio_bytes = await self._client.download(url, op="tts")
        duration_s, sample_rate = _wav_duration(audio_bytes, _DEFAULT_SAMPLE_RATE)

        words: list[TtsWord] = []
        alignment = "proportional"
        if word_timestamps:
            words, alignment = await self._align(url, text, duration_s)

        self._client.record_usage(
            Usage(
                model=model,
                operation="tts",
                audio_seconds=duration_s,
                request_id=_get(rsp, "request_id"),
            )
        )
        return TtsResult(
            audio_bytes=audio_bytes,
            sample_rate=sample_rate,
            duration_s=round(duration_s, 3),
            word_timestamps=words,
            alignment=alignment,
            voice_id=used_voice,
            model=model,
            audio_format="wav",
        )

    async def _align(
        self,
        audio_url: str,
        text: str,
        duration_s: float,
    ) -> tuple[list[TtsWord], str]:
        """Prefer real ASR forced alignment; fall back to proportional timing."""
        with contextlib.suppress(ProviderError):
            words = await self._align_via_asr(audio_url)
            if words:
                return words, "asr"
        return proportional_alignment(text, duration_s), "proportional"

    async def _align_via_asr(self, audio_url: str) -> list[TtsWord]:
        from dashscope.audio.qwen_asr import QwenTranscription

        call = functools.partial(
            QwenTranscription.call,
            model=_ASR_MODEL,
            file_url=audio_url,
            api_key=self._client.api_key,
        )
        rsp = await self._client.call_sdk(call, op="asr", model=_ASR_MODEL)
        words = _extract_asr_words(_get(rsp, "output"))
        if words:
            self._client.record_usage(
                Usage(model=_ASR_MODEL, operation="asr", request_id=_get(rsp, "request_id"))
            )
        return words

    @staticmethod
    def plan_prosody(text: str) -> ProsodyPlan:
        """Deterministic prosody plan for ``text`` (emphasis + breaks, §9.4).

        Pure, no model call, no spend — see :func:`app.providers.prosody.plan_prosody`.
        The plan's ``style_instruction`` can drive a future opt-in instruct-model
        synthesis; its per-token stress feeds the sync-map highlight pulse. Exposed
        here so callers reach narration prosody through the narration provider.
        """
        return plan_prosody(text)


__all__ = ["TtsProvider", "proportional_alignment", "resolve_tts_model"]
