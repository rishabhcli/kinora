"""Concrete :class:`~app.audio.protocol.UniversalAudioProvider` adapters.

Each adapter declares a real :class:`~app.audio.types.AudioCapability` profile for
its model family and translates the canonical :class:`~app.audio.types.TtsRequest`
into that backend's wire shape:

* :mod:`app.audio.adapters.dashscope` — wraps the existing
  :class:`app.providers.tts.TtsProvider` (CosyVoice / Qwen3-TTS), preserving its
  behaviour while exposing it through the universal seam.
* :mod:`app.audio.adapters.elevenlabs` / :mod:`app.audio.adapters.openai` /
  :mod:`app.audio.adapters.azure` / :mod:`app.audio.adapters.google` — declarative
  profiles for those hosted TTS engines, built on the shared descriptor adapter.
* :mod:`app.audio.adapters.descriptor` — a generic, descriptor-driven adapter:
  give it an :class:`~app.audio.types.AudioCapability` + a pure synth function and
  it becomes a universal provider (the substrate the hosted adapters reuse and the
  fast path for deterministic tests / new backends).
"""

from __future__ import annotations

from .azure import AzureAudioAdapter
from .dashscope import DashScopeAudioAdapter
from .descriptor import (
    DescriptorAudioProvider,
    RawAudio,
    SynthFn,
    azure_profile,
    elevenlabs_profile,
    google_profile,
    openai_profile,
)
from .elevenlabs import ElevenLabsAudioAdapter
from .google import GoogleAudioAdapter
from .openai import OpenAiAudioAdapter

__all__ = [
    "AzureAudioAdapter",
    "DashScopeAudioAdapter",
    "DescriptorAudioProvider",
    "ElevenLabsAudioAdapter",
    "GoogleAudioAdapter",
    "OpenAiAudioAdapter",
    "RawAudio",
    "SynthFn",
    "azure_profile",
    "elevenlabs_profile",
    "google_profile",
    "openai_profile",
]
