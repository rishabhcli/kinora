"""ElevenLabs TTS adapter (universal seam).

A thin, transport-agnostic adapter over :class:`DescriptorAudioProvider`: it owns
the real ElevenLabs capability profile and turns a caller-supplied *synth callable*
(``(TtsRequest) -> RawAudio``) into a universal provider. The synth callable is the
single network seam — in production it wraps an ElevenLabs HTTP call; in tests it is
a deterministic fake. Keeping the HTTP out of this module means the adapter (profile
+ request mapping + alignment fallback) is unit-tested with no network or key.

ElevenLabs emits no inline word timestamps on the standard path, so narration word
timing here always rides the proportional alignment FALLBACK (unless the synth
callable supplies ASR timings).
"""

from __future__ import annotations

from app.audio.adapters.descriptor import (
    DescriptorAudioProvider,
    SynthFn,
    elevenlabs_profile,
)
from app.audio.types import AudioCapability


class ElevenLabsAudioAdapter(DescriptorAudioProvider):
    """ElevenLabs as a :class:`~app.audio.protocol.UniversalAudioProvider`."""

    def __init__(
        self,
        synth: SynthFn,
        *,
        model: str = "eleven_multilingual_v2",
        profile: AudioCapability | None = None,
        name: str | None = None,
    ) -> None:
        super().__init__(
            name=name or f"audio:elevenlabs:{model}",
            profile=profile or elevenlabs_profile(model),
            synth=synth,
        )


__all__ = ["ElevenLabsAudioAdapter"]
