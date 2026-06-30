"""Azure (Cognitive Services) Neural TTS adapter (universal seam).

Azure is the richest of the hosted profiles: SSML-first (prosody, ``express-as``
emotion, speed + pitch), with word-boundary events — so it *does* support inline
word timestamps. A synth callable that wires the Azure SDK can surface those as
:attr:`RawAudio.word_timings` and they'll be normalized (``model`` provenance); a
callable that omits them falls back to the proportional estimate, same as any other
backend. The HTTP/SDK call stays in the caller's synth closure so the adapter is
network-free and unit-testable.
"""

from __future__ import annotations

from app.audio.adapters.descriptor import (
    DescriptorAudioProvider,
    SynthFn,
    azure_profile,
)
from app.audio.types import AudioCapability


class AzureAudioAdapter(DescriptorAudioProvider):
    """Azure Neural TTS as a :class:`~app.audio.protocol.UniversalAudioProvider`."""

    def __init__(
        self,
        synth: SynthFn,
        *,
        model: str = "azure-neural-tts",
        profile: AudioCapability | None = None,
        name: str | None = None,
    ) -> None:
        super().__init__(
            name=name or f"audio:azure:{model}",
            profile=profile or azure_profile(model),
            synth=synth,
        )


__all__ = ["AzureAudioAdapter"]
