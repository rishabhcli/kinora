"""DashScope CosyVoice / Qwen3-TTS adapter over the existing provider.

Wraps the live :class:`app.providers.tts.TtsProvider` in the universal seam
*without changing its behaviour*: ``synthesize`` calls the same
``TtsProvider.synthesize`` (same model resolution, same voice-fallback, same
real-ASR-then-proportional alignment, same usage accounting) and re-expresses the
returned :class:`~app.providers.types.TtsResult` as a canonical
:class:`~app.audio.types.AudioResult`. So flipping the Generator onto the universal
seam is byte-for-byte identical to today's DashScope narration path — it just gains
the ability to be one backend among many in an :class:`~app.audio.router.AudioRouter`.

The DashScope TTS family does not emit inline word timestamps; the underlying
provider already runs real ASR forced alignment with a proportional fallback and
stamps which path won, so this adapter preserves that provenance faithfully.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from app.audio.alignment import normalize_words
from app.audio.protocol import OneShotAudioProvider
from app.audio.types import (
    AlignmentMethod,
    AudioCapability,
    AudioFormat,
    AudioResult,
    AudioTrackType,
    AudioWord,
    TtsRequest,
)

#: DashScope's documented intl preset voices for the narration TTS family. Used in
#: the capability profile; the underlying provider also voice-falls-back to
#: ``"Cherry"`` when a snapshot rejects an assigned id.
_DASHSCOPE_VOICES = ("Cherry", "Ryan", "Serena", "Ethan", "Chelsie")
_DEFAULT_SAMPLE_RATE = 24000


class _TtsWordLike(Protocol):
    text: str
    t_start: float
    t_end: float


class _TtsResultLike(Protocol):
    audio_bytes: bytes
    sample_rate: int
    duration_s: float
    # A read-only Sequence (covariant) so any concrete word type satisfies it.
    @property
    def word_timestamps(self) -> Sequence[_TtsWordLike]: ...
    alignment: str
    voice_id: str | None
    model: str
    audio_format: str


class _TtsProviderLike(Protocol):
    """The slice of :class:`app.providers.tts.TtsProvider` this adapter calls."""

    async def synthesize(
        self,
        text: str,
        *,
        voice_id: str,
        speed: float = ...,
        pitch: float = ...,
        word_timestamps: bool = ...,
        model: str | None = ...,
        language_type: str = ...,
        timeout: float | None = ...,
    ) -> _TtsResultLike: ...


#: DashScope's provider alignment string → our canonical provenance enum.
_ALIGNMENT_MAP = {
    "asr": AlignmentMethod.ASR,
    "model": AlignmentMethod.MODEL,
    "proportional": AlignmentMethod.PROPORTIONAL,
}


class DashScopeAudioAdapter(OneShotAudioProvider):
    """Expose the existing DashScope TTS provider as a universal audio provider."""

    def __init__(
        self,
        provider: _TtsProviderLike,
        *,
        model: str = "qwen3-tts-flash",
        default_voice: str = "Cherry",
        name: str | None = None,
        supports_voice_cloning: bool = True,
    ) -> None:
        self._provider = provider
        self._model = model
        self._default_voice = default_voice
        self._supports_cloning = supports_voice_cloning
        self.name = name or f"audio:dashscope:{model}"
        self._oneshot_cache: dict[str, AudioResult] = {}

    def capabilities(self) -> AudioCapability:
        """The DashScope narration profile (no inline word timing; cloning yes)."""
        return AudioCapability(
            provider="dashscope",
            model=self._model,
            track_types=frozenset({AudioTrackType.NARRATION}),
            voices=_DASHSCOPE_VOICES,
            languages=(),  # multilingual via language_type
            sample_rates=(_DEFAULT_SAMPLE_RATE,),
            formats=(AudioFormat.WAV,),
            supports_streaming=False,
            # Inline timestamps not emitted; the provider forced-aligns via ASR and
            # falls back to proportional, so word timing is always produced.
            supports_word_timestamps=False,
            supports_ssml=False,
            supports_emotion=False,
            supports_speed=True,
            supports_pitch=True,
            supports_voice_cloning=self._supports_cloning,
            max_input_chars=None,
        )

    async def synthesize(self, request: TtsRequest) -> AudioResult:
        """Delegate to the live provider and re-express its result canonically."""
        if request.track_type is not AudioTrackType.NARRATION:
            raise ValueError("DashScope adapter only produces narration tracks")
        result = await self._provider.synthesize(
            request.text,
            voice_id=request.voice_id or self._default_voice,
            speed=request.speed,
            pitch=request.pitch,
            word_timestamps=request.word_timestamps,
            model=request.model or self._model,
            language_type=request.language or "English",
        )
        return self._to_audio_result(result, request)

    def _to_audio_result(self, result: _TtsResultLike, request: TtsRequest) -> AudioResult:
        words: tuple[AudioWord, ...] = ()
        alignment = AlignmentMethod.NONE
        if request.word_timestamps:
            # Re-normalize through the canonical normalizer so the shape matches
            # every other backend (the provider already produced clean timings;
            # this is idempotent and de-overlaps defensively).
            words = normalize_words(
                [(w.text, w.t_start, w.t_end) for w in result.word_timestamps],
                duration_s=result.duration_s,
            )
            alignment = _ALIGNMENT_MAP.get(result.alignment, AlignmentMethod.PROPORTIONAL)
            if not words:
                alignment = AlignmentMethod.NONE
        return AudioResult(
            audio_bytes=result.audio_bytes,
            sample_rate=result.sample_rate or _DEFAULT_SAMPLE_RATE,
            duration_s=result.duration_s,
            track_type=AudioTrackType.NARRATION,
            word_timestamps=words,
            alignment=alignment,
            voice_id=result.voice_id,
            model=result.model,
            provider="dashscope",
            audio_format=AudioFormat(result.audio_format)
            if result.audio_format in AudioFormat.__members__.values()
            or result.audio_format in {f.value for f in AudioFormat}
            else AudioFormat.WAV,
        )


__all__ = ["DashScopeAudioAdapter"]
