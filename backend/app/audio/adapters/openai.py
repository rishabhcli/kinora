"""OpenAI TTS adapter (universal seam).

A thin profile declaration over :class:`DescriptorAudioProvider`. The synth
callable is the only network seam (OpenAI's ``/audio/speech`` in production, a fake
in tests). OpenAI TTS takes a free-text ``instructions`` style field (mapped from
:attr:`TtsRequest.style_instruction`) but emits no word timestamps, so narration
timing rides the alignment FALLBACK.
"""

from __future__ import annotations

from app.audio.adapters.descriptor import (
    DescriptorAudioProvider,
    SynthFn,
    openai_profile,
)
from app.audio.types import AudioCapability


class OpenAiAudioAdapter(DescriptorAudioProvider):
    """OpenAI TTS as a :class:`~app.audio.protocol.UniversalAudioProvider`."""

    def __init__(
        self,
        synth: SynthFn,
        *,
        model: str = "gpt-4o-mini-tts",
        profile: AudioCapability | None = None,
        name: str | None = None,
    ) -> None:
        super().__init__(
            name=name or f"audio:openai:{model}",
            profile=profile or openai_profile(model),
            synth=synth,
        )


__all__ = ["OpenAiAudioAdapter"]
