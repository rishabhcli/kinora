"""Word-timestamp normalizer + forced-alignment FALLBACK (§9.4).

Karaoke highlight + page-turn (§9.4, §5.3) need a clean per-word timing map. But
backends disagree wildly: some emit inline word timestamps, some emit them in ms,
some out of order or overlapping, some none at all. This module is the single
place that turns *any* of those into the one canonical shape the sync map wants:

* :func:`normalize_words` — sanitise a backend's raw word timings: drop empties,
  convert ms→s when the magnitude looks like ms, clamp to ``[0, duration]``, sort
  by start, and de-overlap so highlights never run backwards or collide.
* :func:`estimate_alignment` — the FALLBACK: when a backend returns *no* word
  timing, distribute the *measured* audio duration across the words of the text,
  weighted by token length with a small inter-word gap, so karaoke/page-turn still
  works for every provider. (Mirrors the proven
  :func:`app.providers.tts.proportional_alignment` heuristic, kept independent so
  the audio subsystem has no hard dependency on the provider layer.)
* :func:`align_words` — the policy: prefer normalized model/ASR timings; fall back
  to estimation only when they are absent or unusable. Returns the words **and**
  the :class:`~app.audio.types.AlignmentMethod` provenance.

Everything here is pure (no model calls, no network, no spend) and exhaustively
unit-testable.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from .types import AlignmentMethod, AudioWord

#: A token = a maximal run of non-whitespace (1:1 with the words a TTS/aligner
#: sees and with the prosody planner's tokenisation).
_WORD_RE = re.compile(r"\S+")

#: Above this end-time, a value almost certainly denotes milliseconds, not
#: seconds (no narrated word ends 100 s in). Used to auto-detect ms timings.
_MS_THRESHOLD = 100.0

#: Default share of the duration spent in inter-word gaps so adjacent highlights
#: do not visually run together.
_DEFAULT_GAP_RATIO = 0.06


def tokenize(text: str) -> list[str]:
    """Split ``text`` into whitespace-delimited tokens (the word unit)."""
    return _WORD_RE.findall(text or "")


def _scale_for(words: Sequence[tuple[str, float, float]]) -> float:
    """Return 0.001 when the timings look like milliseconds, else 1.0.

    Heuristic, applied to the *whole* batch so a single short clip is not
    misread: if the largest end-time exceeds :data:`_MS_THRESHOLD`, the batch is
    in ms (DashScope ASR reports ms; several hosted TTS engines do too).
    """
    max_end = max((end for _, _, end in words), default=0.0)
    return 0.001 if max_end > _MS_THRESHOLD else 1.0


def normalize_words(
    raw: Sequence[tuple[str, float, float]] | Sequence[AudioWord],
    *,
    duration_s: float | None = None,
    round_to: int = 3,
) -> tuple[AudioWord, ...]:
    """Normalize raw word timings into the canonical, monotonic sync shape.

    Accepts either ``(text, start, end)`` triples or :class:`AudioWord` s. The
    pipeline: drop empty/blank tokens → auto-detect + apply a ms→s scale over the
    batch → clamp negatives to 0 and (when ``duration_s`` is known) cap to the
    clip → sort by start → de-overlap (each word starts no earlier than the prior
    word's end) → round. Guarantees a non-decreasing, non-overlapping series so
    the karaoke highlight only ever moves forward.
    """
    triples: list[tuple[str, float, float]] = []
    for item in raw:
        if isinstance(item, AudioWord):
            text, start, end = item.text, item.t_start, item.t_end
        else:
            text, start, end = item
        if text and str(text).strip():
            triples.append((str(text), float(start), float(end)))
    if not triples:
        return ()

    scale = _scale_for(triples)
    cap = duration_s if (duration_s is not None and duration_s > 0) else None

    scaled: list[tuple[str, float, float]] = []
    for text, start, end in triples:
        s = max(start * scale, 0.0)
        e = max(end * scale, s)
        if cap is not None:
            s = min(s, cap)
            e = min(e, cap)
        scaled.append((text, s, e))

    scaled.sort(key=lambda t: (t[1], t[2]))

    out: list[AudioWord] = []
    cursor = 0.0
    for text, start, end in scaled:
        s = max(start, cursor)
        e = max(end, s)
        if cap is not None:
            e = min(e, cap)
            s = min(s, e)
        out.append(
            AudioWord(text=text, t_start=round(s, round_to), t_end=round(e, round_to))
        )
        cursor = e
    return tuple(out)


def estimate_alignment(
    text: str,
    duration_s: float,
    *,
    gap_ratio: float = _DEFAULT_GAP_RATIO,
    round_to: int = 3,
) -> tuple[AudioWord, ...]:
    """Estimate per-word timings from text + measured duration (the FALLBACK).

    Distributes the real measured ``duration_s`` across the tokens of ``text``,
    weighted by character length (longer words take proportionally longer), with a
    small inter-word gap so highlights don't collide. The total is anchored to the
    real waveform length, so even a model with no word timing drives a usable
    karaoke highlight + page-turn. Empty text or non-positive duration → no words.
    """
    tokens = tokenize(text)
    if not tokens or duration_s <= 0:
        return ()
    gap_ratio = min(max(gap_ratio, 0.0), 0.9)
    weights = [len(tok) + 1 for tok in tokens]
    total_weight = sum(weights)
    speech = duration_s * (1.0 - gap_ratio)
    gap = (duration_s * gap_ratio) / max(len(tokens), 1)
    words: list[AudioWord] = []
    cursor = 0.0
    for tok, weight in zip(tokens, weights, strict=True):
        span = speech * (weight / total_weight)
        start = cursor
        end = min(duration_s, start + span)
        words.append(
            AudioWord(text=tok, t_start=round(start, round_to), t_end=round(end, round_to))
        )
        cursor = end + gap
    return tuple(words)


def align_words(
    text: str,
    duration_s: float,
    *,
    model_words: Sequence[tuple[str, float, float]] | Sequence[AudioWord] | None = None,
    method: AlignmentMethod = AlignmentMethod.MODEL,
    require_word_count_match: bool = False,
) -> tuple[tuple[AudioWord, ...], AlignmentMethod]:
    """Resolve the final word map + its provenance (the alignment policy).

    Prefers real ``model_words`` (inline model timestamps or ASR forced
    alignment): they are normalized and, if usable, returned with the given
    ``method``. Falls back to :func:`estimate_alignment` (PROPORTIONAL) when the
    model returned no words, returned an empty set, or — when
    ``require_word_count_match`` is set — returned a word count that disagrees with
    the text (a sign the alignment is unreliable for this utterance).

    Returns ``((), NONE)`` only when there is nothing to align (empty text and no
    model words) — callers treat that as "no narration timing for this track".
    """
    normalized = normalize_words(model_words or (), duration_s=duration_s)
    if normalized:
        if require_word_count_match:
            expected = len(tokenize(text))
            if expected and len(normalized) != expected:
                fallback = estimate_alignment(text, duration_s)
                if fallback:
                    return fallback, AlignmentMethod.PROPORTIONAL
        return normalized, method

    estimated = estimate_alignment(text, duration_s)
    if estimated:
        return estimated, AlignmentMethod.PROPORTIONAL
    return (), AlignmentMethod.NONE


__all__ = [
    "align_words",
    "estimate_alignment",
    "normalize_words",
    "tokenize",
]
