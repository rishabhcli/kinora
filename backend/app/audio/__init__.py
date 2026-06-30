"""Provider-agnostic AUDIO backend abstraction (the audio analogue of the video layer).

Narration / music / SFX can come from *any* model behind one seam:

* :mod:`app.audio.types` — the canonical vocabulary: :class:`AudioCapability`
  (what a backend can do), :class:`TtsRequest` (a provider-neutral synthesis
  request), :class:`AudioResult` (audio + the normalized word-timing map),
  :class:`MusicBedSpec` (the optional music/ambient bed track).
* :mod:`app.audio.protocol` — :class:`UniversalAudioProvider` (capabilities /
  synthesize / healthy + async submit/poll/fetch) and the one-shot mixin.
* :mod:`app.audio.alignment` — the word-timestamp normalizer + the forced-alignment
  FALLBACK that keeps karaoke + page-turn working for backends with no inline timing.
* :mod:`app.audio.adapters` — concrete adapters: DashScope CosyVoice/Qwen3-TTS
  (wrapping the existing provider), ElevenLabs, OpenAI, Azure, Google, and a generic
  descriptor-driven adapter.
* :mod:`app.audio.router` — capability-aware, health-based failover across backends.
* :mod:`app.audio.seam` — :class:`NarrationSeam`, the literal drop-in for the
  Generator's ``providers.tts`` narration call.

Additive and self-contained: nothing here imports into the existing render/agent
paths until a caller wires :class:`NarrationSeam` in. No network, no spend.
"""

from __future__ import annotations

from .adapters import (
    AzureAudioAdapter,
    DashScopeAudioAdapter,
    DescriptorAudioProvider,
    ElevenLabsAudioAdapter,
    GoogleAudioAdapter,
    OpenAiAudioAdapter,
    RawAudio,
    SynthFn,
    azure_profile,
    elevenlabs_profile,
    google_profile,
    openai_profile,
)
from .alignment import align_words, estimate_alignment, normalize_words, tokenize
from .protocol import (
    AudioJob,
    JobStatus,
    OneShotAudioProvider,
    UniversalAudioProvider,
    wav_duration,
)
from .router import (
    AudioRouter,
    AudioRouterPolicy,
    BackendHealth,
    BackendStatus,
    can_serve,
)
from .seam import NarrationSeam, to_tts_result
from .types import (
    AlignmentMethod,
    AudioCapability,
    AudioFormat,
    AudioResult,
    AudioTrackType,
    AudioWord,
    MusicBedSpec,
    TtsRequest,
    words_to_tuple,
)

__all__ = [
    "AlignmentMethod",
    "AudioCapability",
    "AudioFormat",
    "AudioJob",
    "AudioResult",
    "AudioRouter",
    "AudioRouterPolicy",
    "AudioTrackType",
    "AudioWord",
    "AzureAudioAdapter",
    "BackendHealth",
    "BackendStatus",
    "DashScopeAudioAdapter",
    "DescriptorAudioProvider",
    "ElevenLabsAudioAdapter",
    "GoogleAudioAdapter",
    "JobStatus",
    "MusicBedSpec",
    "NarrationSeam",
    "OneShotAudioProvider",
    "OpenAiAudioAdapter",
    "RawAudio",
    "SynthFn",
    "TtsRequest",
    "UniversalAudioProvider",
    "align_words",
    "azure_profile",
    "can_serve",
    "elevenlabs_profile",
    "estimate_alignment",
    "google_profile",
    "normalize_words",
    "openai_profile",
    "to_tts_result",
    "tokenize",
    "wav_duration",
    "words_to_tuple",
]
