"""A generic, descriptor-driven :class:`UniversalAudioProvider`.

The substrate every hosted TTS adapter reuses: give it an
:class:`~app.audio.types.AudioCapability` profile and a pure async *synth function*
``(TtsRequest) -> RawAudio`` that returns the model's bytes (+ any inline word
timings it emits), and you get a complete universal provider — capability gating,
WAV-duration measurement, word-timestamp normalization, and the forced-alignment
FALLBACK are all handled here, once, for every backend.

This is also how a brand-new backend or a deterministic test "becomes" an audio
provider without a bespoke class: wrap a closure. The ElevenLabs / OpenAI / Azure /
Google adapters are thin profile declarations over this class.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from app.audio.alignment import align_words
from app.audio.protocol import OneShotAudioProvider, wav_duration
from app.audio.types import (
    AlignmentMethod,
    AudioCapability,
    AudioFormat,
    AudioResult,
    AudioTrackType,
    AudioWord,
    TtsRequest,
)


@dataclass(frozen=True, slots=True)
class RawAudio:
    """What a backend's synth function returns: bytes + optional inline timing.

    ``word_timings`` are the model's *raw* ``(text, start, end)`` triples (any
    units, any order); the adapter normalizes them. Leave them empty to force the
    alignment fallback. ``sample_rate`` 0 means "measure it from the WAV".
    """

    audio_bytes: bytes
    sample_rate: int = 0
    audio_format: AudioFormat = AudioFormat.WAV
    word_timings: tuple[tuple[str, float, float], ...] = ()
    #: True when ``word_timings`` came from real ASR (vs. the model emitting them
    #: inline); selects the ``asr`` provenance over ``model``.
    from_asr: bool = False
    voice_id: str | None = None
    provider_job_id: str | None = None


#: A pure async synthesis function: canonical request in, raw model audio out.
SynthFn = Callable[[TtsRequest], Awaitable[RawAudio]]


@dataclass(slots=True)
class DescriptorAudioProvider(OneShotAudioProvider):
    """A universal audio provider defined by a profile + a synth function.

    Validation, duration measurement, normalization, and the alignment fallback
    are uniform here; the ``synth`` closure is the only backend-specific part.
    """

    name: str
    profile: AudioCapability
    synth: SynthFn
    #: Probe used by :meth:`healthy`; default always-healthy (no network).
    health_probe: Callable[[], Awaitable[bool]] | None = field(default=None, repr=False)
    _oneshot_cache: dict[str, AudioResult] = field(default_factory=dict, repr=False)

    def capabilities(self) -> AudioCapability:
        """The declared capability profile for this backend."""
        return self.profile

    async def healthy(self) -> bool:
        """Liveness: defer to ``health_probe`` if given, else always routable."""
        if self.health_probe is None:
            return True
        return await self.health_probe()

    async def synthesize(self, request: TtsRequest) -> AudioResult:
        """Synthesize ``request``, normalize timing, and apply the fallback.

        Validates the request against the profile, calls the synth function,
        measures the real audio duration, then resolves word timings: inline
        model/ASR timings are normalized; their absence triggers the proportional
        estimate so karaoke/page-turn always has a map (for NARRATION tracks).
        """
        self._validate(request)
        raw = await self.synth(request)
        default_sr = self.profile.default_sample_rate()
        duration_s, measured_sr = wav_duration(raw.audio_bytes, default_sr)
        sample_rate = raw.sample_rate or measured_sr or default_sr

        words: tuple[AudioWord, ...] = ()
        alignment = AlignmentMethod.NONE
        if request.track_type is AudioTrackType.NARRATION and request.word_timestamps:
            method = AlignmentMethod.ASR if raw.from_asr else AlignmentMethod.MODEL
            words, alignment = align_words(
                request.text,
                duration_s,
                model_words=raw.word_timings,
                method=method,
            )

        return AudioResult(
            audio_bytes=raw.audio_bytes,
            sample_rate=sample_rate,
            duration_s=round(duration_s, 3),
            track_type=request.track_type,
            word_timestamps=words,
            alignment=alignment,
            voice_id=raw.voice_id or request.voice_id,
            model=request.model or self.profile.model,
            provider=self.profile.provider,
            audio_format=raw.audio_format,
            provider_job_id=raw.provider_job_id,
        )

    def _validate(self, request: TtsRequest) -> None:
        """Reject a request this backend cannot honour (cheap, before spend)."""
        prof = self.profile
        if not prof.supports_track(request.track_type):
            raise ValueError(
                f"{self.name} cannot produce a {request.track_type.value} track"
            )
        if not prof.supports_voice(request.voice_id):
            raise ValueError(f"{self.name} does not support voice {request.voice_id!r}")
        if not prof.supports_language(request.language):
            raise ValueError(f"{self.name} does not support language {request.language!r}")
        if request.is_ssml and not prof.supports_ssml:
            raise ValueError(f"{self.name} does not accept SSML input")
        if request.voice_clone_reference is not None and not prof.supports_voice_cloning:
            raise ValueError(f"{self.name} does not support voice cloning")
        if prof.max_input_chars is not None and request.char_count() > prof.max_input_chars:
            raise ValueError(
                f"{self.name} input is {request.char_count()} chars; "
                f"limit is {prof.max_input_chars}"
            )


# --------------------------------------------------------------------------- #
# Hosted-engine capability profiles (declarative; real documented limits)
# --------------------------------------------------------------------------- #


def elevenlabs_profile(model: str = "eleven_multilingual_v2") -> AudioCapability:
    """Capability profile for ElevenLabs TTS.

    ElevenLabs streams, clones voices, and exposes style/stability controls, but
    does not emit inline word-level timestamps on the standard synth path (its
    character-level alignment is a separate endpoint), so word timing here rides
    the alignment fallback unless the caller supplies ASR.
    """
    return AudioCapability(
        provider="elevenlabs",
        model=model,
        track_types=frozenset({AudioTrackType.NARRATION}),
        languages=(),  # multilingual
        sample_rates=(44100, 24000, 16000),
        formats=(AudioFormat.MP3, AudioFormat.PCM),
        supports_streaming=True,
        supports_word_timestamps=False,
        supports_ssml=False,
        supports_emotion=True,
        supports_speed=True,
        supports_pitch=False,
        supports_voice_cloning=True,
        max_input_chars=5000,
    )


def openai_profile(model: str = "gpt-4o-mini-tts") -> AudioCapability:
    """Capability profile for OpenAI TTS.

    OpenAI TTS takes a free-text style ``instructions`` field (emotion control),
    streams, and emits several formats, but returns no word timestamps — alignment
    rides the fallback.
    """
    return AudioCapability(
        provider="openai",
        model=model,
        track_types=frozenset({AudioTrackType.NARRATION}),
        voices=("alloy", "echo", "fable", "onyx", "nova", "shimmer"),
        languages=(),
        sample_rates=(24000,),
        formats=(AudioFormat.MP3, AudioFormat.OPUS, AudioFormat.WAV, AudioFormat.FLAC),
        supports_streaming=True,
        supports_word_timestamps=False,
        supports_ssml=False,
        supports_emotion=True,
        supports_speed=True,
        supports_pitch=False,
        supports_voice_cloning=False,
        max_input_chars=4096,
    )


def azure_profile(model: str = "azure-neural-tts") -> AudioCapability:
    """Capability profile for Azure (Cognitive Services) Neural TTS.

    Azure is SSML-first (prosody, emotion ``mstts:express-as``, speed/pitch all via
    SSML) and emits word-boundary events, so it *does* support inline word
    timestamps. A rich, fully-featured profile.
    """
    return AudioCapability(
        provider="azure",
        model=model,
        track_types=frozenset({AudioTrackType.NARRATION}),
        voices=("en-US-JennyNeural", "en-US-GuyNeural", "en-GB-SoniaNeural"),
        languages=("en-US", "en-GB", "es-ES", "fr-FR", "de-DE", "zh-CN", "ja-JP"),
        sample_rates=(48000, 24000, 16000),
        formats=(AudioFormat.MP3, AudioFormat.WAV, AudioFormat.OGG, AudioFormat.OPUS),
        supports_streaming=True,
        supports_word_timestamps=True,
        supports_ssml=True,
        supports_emotion=True,
        supports_speed=True,
        supports_pitch=True,
        supports_voice_cloning=False,
        max_input_chars=10000,
    )


def google_profile(model: str = "google-neural2") -> AudioCapability:
    """Capability profile for Google Cloud Text-to-Speech.

    Google is SSML-first with speed/pitch, emits ``timepoint`` marks (so word
    timing is supported when ``<mark>`` tags are inserted), and supports a wide
    language set.
    """
    return AudioCapability(
        provider="google",
        model=model,
        track_types=frozenset({AudioTrackType.NARRATION}),
        voices=("en-US-Neural2-A", "en-US-Neural2-C", "en-GB-Neural2-B"),
        languages=("en-US", "en-GB", "es-ES", "fr-FR", "de-DE", "zh-CN", "ja-JP", "hi-IN"),
        sample_rates=(48000, 24000, 16000),
        formats=(AudioFormat.MP3, AudioFormat.WAV, AudioFormat.OGG),
        supports_streaming=True,
        supports_word_timestamps=True,
        supports_ssml=True,
        supports_emotion=False,
        supports_speed=True,
        supports_pitch=True,
        supports_voice_cloning=False,
        max_input_chars=5000,
    )


__all__ = [
    "DescriptorAudioProvider",
    "RawAudio",
    "SynthFn",
    "azure_profile",
    "elevenlabs_profile",
    "google_profile",
    "openai_profile",
]
