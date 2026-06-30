"""Google Cloud Text-to-Speech adapter (universal seam).

Google is SSML-first with speed/pitch and emits ``timepoint`` marks (so word timing
is available when ``<mark>`` tags are inserted). The synth callable owns the
network/SDK call; supplied timepoints surface as :attr:`RawAudio.word_timings`
(normalized as ``model`` provenance), and their absence triggers the proportional
alignment FALLBACK. Network-free and unit-testable.
"""

from __future__ import annotations

from app.audio.adapters.descriptor import (
    DescriptorAudioProvider,
    SynthFn,
    google_profile,
)
from app.audio.types import AudioCapability


class GoogleAudioAdapter(DescriptorAudioProvider):
    """Google Cloud TTS as a :class:`~app.audio.protocol.UniversalAudioProvider`."""

    def __init__(
        self,
        synth: SynthFn,
        *,
        model: str = "google-neural2",
        profile: AudioCapability | None = None,
        name: str | None = None,
    ) -> None:
        super().__init__(
            name=name or f"audio:google:{model}",
            profile=profile or google_profile(model),
            synth=synth,
        )


__all__ = ["GoogleAudioAdapter"]
