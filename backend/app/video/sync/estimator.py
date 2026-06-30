"""Forced-alignment *estimator* for backends that return no word timings (§9.4).

Some video+audio backends emit a clip and a duration but **no** word-level timing
(a TTS without alignment, a pre-rendered narration track, or a model that only
returns total length). Karaoke still needs a per-word ``[t_start, t_end]``. This
module distributes the spoken text across the *measured* clip duration by a
linguistic weight (syllables + punctuation pauses), giving a believable highlight
sweep anchored to the real audio length — the §9.4 ``proportional`` fallback, made
punctuation-aware and reusable across backends.

It is a strict generalization of :func:`app.providers.tts.proportional_alignment`
(which weighs by raw character count with a flat inter-word gap): here the weight
is syllable-based and the gap is *where the punctuation is*, so a sentence's final
word actually lingers over its full stop. Pure and deterministic.
"""

from __future__ import annotations

from .models import WordTiming
from .text import tokenize, word_weight

#: Floor on a word's rendered span so an ultra-fast clip never yields zero-width
#: (un-highlightable) words; absolute spans are still clamped to the duration.
_MIN_WORD_S = 0.01


def estimate_word_timings(
    text: str,
    *,
    duration_s: float,
    lead_in_s: float = 0.0,
) -> list[WordTiming]:
    """Distribute ``text`` across ``duration_s`` by syllable + punctuation weight.

    Args:
        text: the spoken narration text (whitespace-tokenized into words).
        duration_s: the **measured** audio/clip duration to spread the words over.
        lead_in_s: optional silence before the first word (e.g. a clip that opens on
            a beat of music); the words fill ``[lead_in_s, duration_s]``.

    Returns:
        One :class:`WordTiming` per token, each marked ``estimated=True``,
        contiguous and monotonic, with the last word ending exactly at
        ``duration_s``. Returns ``[]`` for empty text or non-positive duration.

    The "gap" between words is folded into the *preceding* word's weight via its
    trailing punctuation pause, so the silence lands after commas/periods (where a
    reader actually pauses) instead of being smeared uniformly. The final word
    carries no trailing-pause weight (nothing follows it).
    """
    tokens = tokenize(text)
    usable = duration_s - max(0.0, lead_in_s)
    if not tokens or usable <= 0.0:
        return []

    last = len(tokens) - 1
    weights = [word_weight(tok, gap_after=(i != last)) for i, tok in enumerate(tokens)]
    total = sum(weights) or float(len(tokens))

    words: list[WordTiming] = []
    cursor = max(0.0, lead_in_s)
    for i, (tok, weight) in enumerate(zip(tokens, weights, strict=True)):
        span = usable * (weight / total)
        start = cursor
        # Snap the final word's end exactly to the duration (no float drift past it).
        end = duration_s if i == last else min(duration_s, start + max(span, _MIN_WORD_S))
        words.append(
            WordTiming(
                text=tok,
                t_start=round(start, 3),
                t_end=round(end, 3),
                estimated=True,
            )
        )
        cursor = end
    return words


__all__ = ["estimate_word_timings"]
