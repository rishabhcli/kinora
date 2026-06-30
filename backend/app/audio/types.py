"""Canonical, provider-agnostic audio request / result / capability models (§9.4).

This is the audio analogue of :mod:`app.providers.types` for the video side: a
single vocabulary every audio backend (DashScope CosyVoice/Qwen3-TTS, ElevenLabs,
OpenAI TTS, Azure, Google, a generic descriptor-driven adapter) speaks, so
narration / music / SFX can come from *any* model without touching the caller.

The three load-bearing shapes:

* :class:`AudioCapability` — what one provider+model can do (voices, languages,
  streaming, inline word timestamps, max chars, sample rates, SSML, emotion/style,
  voice cloning). The router/validator reads this to pick a backend and to know
  whether word timings will need the forced-alignment fallback.
* :class:`TtsRequest` — a fully-resolved synthesis request (text, voice, prosody,
  emotion/style, output format, track type). Canonical and provider-neutral; each
  adapter translates it into its own wire shape.
* :class:`AudioResult` — synthesized audio + the normalized word-timing map that
  drives karaoke + page-turn, plus which alignment path produced it.

Everything here is pure data (pydantic v2 / frozen dataclasses); no model calls,
no network, no spend. The word-timing types deliberately mirror
:class:`app.providers.types.TtsWord` so an :class:`AudioResult` round-trips to the
existing :class:`~app.providers.types.TtsResult` the Generator already consumes.
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

# --------------------------------------------------------------------------- #
# Track type + alignment provenance
# --------------------------------------------------------------------------- #


class AudioTrackType(StrEnum):
    """What an :class:`AudioResult` is — drives mixing + sync downstream.

    NARRATION rides the karaoke/page-turn sync map (word timings matter). MUSIC
    and AMBIENT are beds laid *under* narration (no word timing); SFX is a short
    one-shot cue. The optional bed types let a music/ambient model implement the
    same :class:`~app.audio.protocol.UniversalAudioProvider` Protocol.
    """

    NARRATION = "narration"
    MUSIC = "music"
    AMBIENT = "ambient"
    SFX = "sfx"


class AlignmentMethod(StrEnum):
    """How an :class:`AudioResult`'s word timings were derived (provenance).

    Mirrors and extends the :class:`app.providers.types.TtsResult.alignment`
    literal so a result can declare exactly which path produced its timing.
    """

    #: Timestamps emitted inline by the TTS model itself (most precise).
    MODEL = "model"
    #: Real forced alignment (e.g. ASR) run over the synthesized waveform.
    ASR = "asr"
    #: Estimated from text + measured duration, weighted by token length, when no
    #: model/ASR word-level timing is available (the FALLBACK that keeps karaoke
    #: + page-turn working for *any* backend).
    PROPORTIONAL = "proportional"
    #: No word timing at all (a music/ambient/SFX track, or alignment disabled).
    NONE = "none"


# --------------------------------------------------------------------------- #
# Word timing
# --------------------------------------------------------------------------- #


class AudioWord(BaseModel):
    """One word's timing in synthesized narration (seconds from clip start).

    Structurally identical to :class:`app.providers.types.TtsWord` so the two
    interconvert losslessly; carried separately so the audio subsystem does not
    depend on the provider layer's model.
    """

    model_config = ConfigDict(frozen=True)

    text: str
    t_start: float
    t_end: float

    @property
    def duration(self) -> float:
        """Span of the word in seconds (never negative)."""
        return max(self.t_end - self.t_start, 0.0)


# --------------------------------------------------------------------------- #
# Output format
# --------------------------------------------------------------------------- #


class AudioFormat(StrEnum):
    """Container/codec of the returned audio bytes."""

    WAV = "wav"
    MP3 = "mp3"
    PCM = "pcm"
    OPUS = "opus"
    FLAC = "flac"
    OGG = "ogg"


# --------------------------------------------------------------------------- #
# Capability profile
# --------------------------------------------------------------------------- #


class AudioCapability(BaseModel):
    """The canonical capability profile for one audio provider+model.

    The router uses this to choose a backend and to know, *before* synthesizing,
    whether the result's word timings will be inline (``supports_word_timestamps``)
    or will need the forced-alignment fallback. Profiles are declared by each
    adapter from the real model's documented limits.
    """

    model_config = ConfigDict(frozen=True)

    #: Stable provider identity, e.g. ``"dashscope"`` / ``"elevenlabs"``.
    provider: str
    #: The concrete model id this profile describes.
    model: str
    #: Track types this backend can produce.
    track_types: frozenset[AudioTrackType] = Field(
        default_factory=lambda: frozenset({AudioTrackType.NARRATION})
    )
    #: Preset voice ids the backend exposes (empty = open / cloning-only).
    voices: tuple[str, ...] = ()
    #: BCP-47-ish language tags the backend supports; empty = multilingual/unknown.
    languages: tuple[str, ...] = ()
    #: Output sample rates (Hz) the backend can emit; first = preferred default.
    sample_rates: tuple[int, ...] = (24000,)
    #: Output container formats the backend can emit; first = preferred default.
    formats: tuple[AudioFormat, ...] = (AudioFormat.WAV,)
    #: True when the backend can stream partial audio (chunked synthesis).
    supports_streaming: bool = False
    #: True when the backend emits *inline* word-level timestamps (no aligner
    #: needed); False means a result needs ASR or the proportional fallback.
    supports_word_timestamps: bool = False
    #: True when the backend accepts SSML markup in the request text.
    supports_ssml: bool = False
    #: True when the backend exposes emotion/style control (a style instruction
    #: or a discrete emotion id).
    supports_emotion: bool = False
    #: True when the backend honours numeric speed control.
    supports_speed: bool = False
    #: True when the backend honours numeric pitch control.
    supports_pitch: bool = False
    #: True when the backend can enroll + synthesize a cloned voice.
    supports_voice_cloning: bool = False
    #: Max input characters per request (None = unbounded / unknown).
    max_input_chars: int | None = None
    #: True when synthesis is an async submit→poll→fetch job (slow models), so the
    #: router/caller uses the job seam instead of the one-shot ``synthesize``.
    is_async_job: bool = False

    def supports_voice(self, voice_id: str | None) -> bool:
        """True when ``voice_id`` is usable: a known preset, cloning, or open set.

        ``None`` (let the backend pick its default) is always allowed. A backend
        with no enumerated ``voices`` is treated as open (accepts any id), as is a
        cloning-capable backend (a clone id is not a preset).
        """
        if voice_id is None or not self.voices:
            return True
        if voice_id in self.voices:
            return True
        return self.supports_voice_cloning

    def supports_language(self, language: str | None) -> bool:
        """True when ``language`` is supported (None / empty profile = any)."""
        if language is None or not self.languages:
            return True
        return language in self.languages

    def supports_track(self, track: AudioTrackType) -> bool:
        """True when this backend can produce the given track type."""
        return track in self.track_types

    def default_sample_rate(self) -> int:
        """The preferred output sample rate."""
        return self.sample_rates[0]

    def default_format(self) -> AudioFormat:
        """The preferred output container format."""
        return self.formats[0]


# --------------------------------------------------------------------------- #
# Request
# --------------------------------------------------------------------------- #


class TtsRequest(BaseModel):
    """A fully-resolved, provider-neutral synthesis request.

    Each adapter translates this into its own wire shape; fields a backend cannot
    honour are recorded but dropped (see :class:`AudioCapability`). ``track_type``
    selects narration vs. a music/ambient/SFX bed.
    """

    model_config = ConfigDict(frozen=True)

    text: str = ""
    #: Backend voice id (preset or clone). ``None`` = the backend's default voice.
    voice_id: str | None = None
    #: Override the backend's configured model id for this request.
    model: str | None = None
    #: BCP-47-ish language hint, e.g. ``"English"`` / ``"en-US"``.
    language: str | None = None
    track_type: AudioTrackType = AudioTrackType.NARRATION
    #: Speech rate multiplier (1.0 = natural); honoured iff ``supports_speed``.
    speed: float = 1.0
    #: Pitch multiplier (1.0 = natural); honoured iff ``supports_pitch``.
    pitch: float = 1.0
    #: Volume gain multiplier (1.0 = unchanged) applied by the backend when able.
    volume: float = 1.0
    #: Free-text style/emotion instruction (e.g. "Read warmly, urgent"); honoured
    #: iff ``supports_emotion``. Built deterministically from a prosody plan.
    style_instruction: str | None = None
    #: Discrete emotion id (backend-specific, e.g. ``"calm"``) when the backend
    #: takes an enum rather than free text.
    emotion: str | None = None
    #: True when ``text`` is SSML; honoured iff ``supports_ssml``.
    is_ssml: bool = False
    #: Request inline word timestamps (and accept the alignment fallback when the
    #: backend cannot emit them). Ignored for non-narration tracks.
    word_timestamps: bool = True
    #: Desired output sample rate (Hz); ``None`` = the backend default.
    sample_rate: int | None = None
    #: Desired output format; ``None`` = the backend default.
    audio_format: AudioFormat | None = None
    #: A short reference clip (bytes) for one-shot voice cloning when supported.
    voice_clone_reference: bytes | None = None
    #: Carried through for idempotency / telemetry; never sent to the API.
    shot_id: str | None = None

    def char_count(self) -> int:
        """Number of input characters (the limit-check unit)."""
        return len(self.text)


# --------------------------------------------------------------------------- #
# Result
# --------------------------------------------------------------------------- #


class AudioResult(BaseModel):
    """Synthesized audio + the normalized word-timing map (§9.4).

    The canonical return of every backend. ``word_timestamps`` is always
    *normalized* (sorted, non-overlapping, seconds) regardless of which alignment
    path produced it, so the sync map / karaoke layer never branches on provider.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    audio_bytes: bytes
    sample_rate: int
    duration_s: float
    track_type: AudioTrackType = AudioTrackType.NARRATION
    word_timestamps: tuple[AudioWord, ...] = ()
    alignment: AlignmentMethod = AlignmentMethod.NONE
    voice_id: str | None = None
    model: str
    provider: str
    audio_format: AudioFormat = AudioFormat.WAV
    #: Async backends record their provider-side job id for cross-referencing.
    provider_job_id: str | None = None

    def words_as_dicts(self) -> list[dict[str, float | str]]:
        """The word map as plain dicts (the §9.4 sync-map ``words`` shape)."""
        return [
            {"text": w.text, "t_start": w.t_start, "t_end": w.t_end}
            for w in self.word_timestamps
        ]


# --------------------------------------------------------------------------- #
# Music / ambient bed
# --------------------------------------------------------------------------- #


class MusicBedSpec(BaseModel):
    """A request for a music / ambient bed track laid under narration.

    The optional bed track type: a music/ambient-gen provider implements the same
    :class:`~app.audio.protocol.UniversalAudioProvider` Protocol and returns an
    :class:`AudioResult` with ``track_type`` MUSIC/AMBIENT (no word timing). The
    mix stage (``app.render.audio_post``) lays it under the narration; this spec
    is the canonical, provider-neutral description of what to generate.
    """

    model_config = ConfigDict(frozen=True)

    #: A natural-language description of the bed, e.g. "soft tense strings".
    prompt: str = ""
    track_type: AudioTrackType = AudioTrackType.MUSIC
    #: Target duration in seconds (the bed loops/trims to fit the narration).
    duration_s: float = 5.0
    #: Override the backend's configured model id.
    model: str | None = None
    #: 0..1 loudness the bed should sit at *under* narration (ducking target).
    bed_gain: float = 0.2
    #: Whether the bed should loop to fill the duration vs. one-shot.
    loop: bool = False
    #: Carried through for idempotency / telemetry.
    cue_id: str | None = None

    def to_request(self) -> TtsRequest:
        """Express this bed as a :class:`TtsRequest` for the unified seam.

        A music/ambient backend's ``synthesize`` takes the same request shape as
        narration; the ``prompt`` rides in ``text`` and word timing is off.
        """
        return TtsRequest(
            text=self.prompt,
            model=self.model,
            track_type=self.track_type,
            word_timestamps=False,
            volume=self.bed_gain,
        )


def words_to_tuple(words: Sequence[AudioWord]) -> tuple[AudioWord, ...]:
    """Coerce any word sequence to the canonical immutable tuple."""
    return tuple(words)


__all__ = [
    "AlignmentMethod",
    "AudioCapability",
    "AudioFormat",
    "AudioResult",
    "AudioTrackType",
    "AudioWord",
    "MusicBedSpec",
    "TtsRequest",
    "words_to_tuple",
]
