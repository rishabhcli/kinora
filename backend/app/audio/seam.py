"""The Generator drop-in: a :class:`UniversalAudioProvider` behind the tts seam.

``app.agents.generator.Generator`` calls its narration provider as::

    narration = await self._providers.tts.synthesize(text, voice_id=voice_id)
    ... narration.audio_bytes, narration.sample_rate, narration.word_timestamps

:class:`NarrationSeam` re-presents *any* :class:`UniversalAudioProvider` (a single
adapter or a whole :class:`~app.audio.router.AudioRouter`) under exactly that
signature, returning an object structurally identical to
:class:`app.providers.types.TtsResult` (same attributes the Generator reads). So
swapping the Generator from ``providers.tts`` to a multi-provider audio backend is a
one-line injection — no Generator change — which is the whole point of the universal
abstraction.

The conversion to the provider layer's :class:`~app.providers.types.TtsWord` /
:class:`~app.providers.types.TtsResult` is exact, so the word-timing map the sync
map consumes (§9.4) is byte-identical regardless of which model produced it.
"""

from __future__ import annotations

from typing import Literal

from app.providers.types import TtsResult, TtsWord

from .protocol import UniversalAudioProvider
from .types import AlignmentMethod, AudioResult, TtsRequest

#: Canonical alignment provenance → the provider layer's narrower literal. ``none``
#: has no TtsResult equivalent (the field is non-optional); it maps to
#: ``proportional`` because an empty word list with that label is harmless.
_ALIGNMENT_TO_LITERAL: dict[AlignmentMethod, Literal["asr", "model", "proportional"]] = {
    AlignmentMethod.MODEL: "model",
    AlignmentMethod.ASR: "asr",
    AlignmentMethod.PROPORTIONAL: "proportional",
    AlignmentMethod.NONE: "proportional",
}


def to_tts_result(result: AudioResult) -> TtsResult:
    """Convert a canonical :class:`AudioResult` to a provider :class:`TtsResult`.

    Exact field-for-field mapping; the word timings become provider
    :class:`TtsWord` s in order. Lets a universal backend feed any code path that
    already expects a ``TtsResult`` (the Generator, the sync-map builder).
    """
    return TtsResult(
        audio_bytes=result.audio_bytes,
        sample_rate=result.sample_rate,
        duration_s=result.duration_s,
        word_timestamps=[
            TtsWord(text=w.text, t_start=w.t_start, t_end=w.t_end)
            for w in result.word_timestamps
        ],
        alignment=_ALIGNMENT_TO_LITERAL[result.alignment],
        voice_id=result.voice_id,
        model=result.model,
        audio_format=result.audio_format.value,
    )


class NarrationSeam:
    """Adapt a :class:`UniversalAudioProvider` to the Generator's tts signature.

    Drop-in for ``providers.tts``: ``await seam.synthesize(text, voice_id=...)``
    returns a :class:`~app.providers.types.TtsResult`. Extra knobs (speed/pitch,
    language, style) map onto the canonical :class:`TtsRequest`; backends that
    cannot honour them ignore them (per their capability profile).
    """

    def __init__(self, backend: UniversalAudioProvider) -> None:
        self._backend = backend

    @property
    def backend(self) -> UniversalAudioProvider:
        """The underlying universal provider (adapter or router)."""
        return self._backend

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
        """Synthesize narration, returning a provider-shaped :class:`TtsResult`.

        Mirrors :meth:`app.providers.tts.TtsProvider.synthesize`'s signature so the
        Generator's call site is unchanged. ``timeout`` is accepted for signature
        parity (per-attempt timeouts live inside the concrete backend transport).
        """
        request = TtsRequest(
            text=text,
            voice_id=voice_id,
            model=model,
            language=language_type,
            speed=speed,
            pitch=pitch,
            word_timestamps=word_timestamps,
        )
        result = await self._backend.synthesize(request)
        return to_tts_result(result)


__all__ = ["NarrationSeam", "to_tts_result"]
