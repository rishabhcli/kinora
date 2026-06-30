"""Normalize any provider timing shape into one canonical word timeline (§9.4).

Every video+audio backend reports (or omits) timings differently:

* **per-word** — ``[{text, t_start, t_end}]`` (CosyVoice/Qwen ASR, ElevenLabs words);
* **per-char** — one entry per grapheme (ElevenLabs ``character_alignment``, Whisper
  char-mode), coalesced into words at whitespace;
* **per-token** — sub-word pieces (Whisper/LLM streams) re-joined into words by the
  leading-space (``▁``/``Ġ``/`` x``) and word-piece (``##``) conventions;
* **cue** — SRT / WebVTT phrase cues, words distributed inside each cue window;
* **none** — no timings at all → the forced-alignment estimator runs.

:func:`ingest_timings` is the one entry point. You either tell it the shape, or it
*sniffs* it. The output is always a clean :class:`WordTiming` list on the audio
clock — the single shape every later stage (retime, build, export) consumes.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from .estimator import estimate_word_timings
from .models import RawCue, TimingShape, WordTiming
from .text import tokenize, word_weight

# --------------------------------------------------------------------------- #
# Field-name tolerance (providers spell the same thing many ways)
# --------------------------------------------------------------------------- #

_TEXT_KEYS = ("text", "word", "token", "char", "value", "content")
_START_KEYS = ("t_start", "start", "start_s", "begin", "from", "startTime", "start_time")
_END_KEYS = ("t_end", "end", "end_s", "to", "stop", "endTime", "end_time")
#: Some providers report a start + a duration instead of start + end.
_DUR_KEYS = ("duration", "duration_s", "dur", "length")
#: Word-piece continuation markers (BERT/Whisper) — these glue onto the prior word.
_CONTINUATION_RE = re.compile(r"^(##|@@)")
#: Leading-space sentinels that mark the *start* of a new word in token streams.
_LEADING_SPACE = ("▁", "Ġ", " ")  # ▁ (SentencePiece), Ġ (GPT-2 BPE), real space


def _first(mapping: Mapping[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in mapping and mapping[key] is not None:
            return mapping[key]
    return None


def _scale_to_seconds(value: float, unit: str) -> float:
    """Convert a raw time to seconds for the given ``unit`` (s / ms)."""
    return value / 1000.0 if unit == "ms" else value


def _entry_text(entry: Mapping[str, Any] | Any) -> str:
    if isinstance(entry, Mapping):
        return str(_first(entry, _TEXT_KEYS) or "")
    return str(getattr(entry, "text", "") or "")


def _entry_times(entry: Mapping[str, Any] | Any, unit: str) -> tuple[float, float]:
    if isinstance(entry, Mapping):
        raw_start = _first(entry, _START_KEYS)
        raw_end = _first(entry, _END_KEYS)
        raw_dur = _first(entry, _DUR_KEYS)
    else:
        raw_start = getattr(entry, "t_start", None)
        raw_end = getattr(entry, "t_end", None)
        raw_dur = None
    start = _scale_to_seconds(float(raw_start), unit) if raw_start is not None else 0.0
    if raw_end is not None:
        end = _scale_to_seconds(float(raw_end), unit)
    elif raw_dur is not None:
        end = start + _scale_to_seconds(float(raw_dur), unit)
    else:
        end = start
    return start, end


# --------------------------------------------------------------------------- #
# Per-shape normalizers
# --------------------------------------------------------------------------- #


def _ingest_per_word(entries: Sequence[Any], *, unit: str) -> list[WordTiming]:
    out: list[WordTiming] = []
    for entry in entries:
        text = _entry_text(entry)
        if not text.strip():
            continue
        start, end = _entry_times(entry, unit)
        out.append(WordTiming(text=text, t_start=round(start, 3), t_end=round(max(start, end), 3)))
    return out


def _ingest_per_char(entries: Sequence[Any], *, unit: str) -> list[WordTiming]:
    """Coalesce per-character timings into words, breaking on whitespace.

    A word spans from its first non-space char's start to its last char's end;
    runs of whitespace separate words and are dropped from the painted text.
    """
    out: list[WordTiming] = []
    buf: list[str] = []
    w_start: float | None = None
    w_end = 0.0
    for entry in entries:
        ch = _entry_text(entry)
        start, end = _entry_times(entry, unit)
        if ch.strip() == "":
            if buf:
                out.append(
                    WordTiming(
                        text="".join(buf),
                        t_start=round(w_start or 0.0, 3),
                        t_end=round(max(w_start or 0.0, w_end), 3),
                    )
                )
                buf, w_start = [], None
            continue
        if w_start is None:
            w_start = start
        buf.append(ch)
        w_end = max(w_end, end)
    if buf:
        out.append(
            WordTiming(
                text="".join(buf),
                t_start=round(w_start or 0.0, 3),
                t_end=round(max(w_start or 0.0, w_end), 3),
            )
        )
    return out


def _ingest_per_token(entries: Sequence[Any], *, unit: str) -> list[WordTiming]:
    """Re-join sub-word tokens into words.

    A token *starts a new word* when it carries a leading-space sentinel (▁/Ġ/space)
    and *continues* the current word when it carries a ``##``/``@@`` marker or has no
    leading space. The word's span runs from its first token's start to its last
    token's end.
    """
    out: list[WordTiming] = []
    buf: list[str] = []
    w_start: float | None = None
    w_end = 0.0

    def flush() -> None:
        nonlocal buf, w_start, w_end
        if buf:
            joined = "".join(buf).strip()
            if joined:
                out.append(
                    WordTiming(
                        text=joined,
                        t_start=round(w_start or 0.0, 3),
                        t_end=round(max(w_start or 0.0, w_end), 3),
                    )
                )
        buf, w_start, w_end = [], None, 0.0

    for entry in entries:
        raw = _entry_text(entry)
        if not raw:
            continue
        start, end = _entry_times(entry, unit)
        is_continuation = bool(_CONTINUATION_RE.match(raw))
        starts_word = raw.startswith(_LEADING_SPACE)
        piece = _CONTINUATION_RE.sub("", raw)
        for sentinel in _LEADING_SPACE:
            if piece.startswith(sentinel):
                piece = piece[len(sentinel) :]
        # A leading-space token begins a fresh word (flush the prior one first);
        # a continuation/##-token glues onto the current word.
        if (starts_word and not is_continuation) and buf:
            flush()
        if w_start is None:
            w_start = start
        buf.append(piece)
        w_end = max(w_end, end)
    flush()
    return out


def words_from_cue(cue: RawCue) -> list[WordTiming]:
    """Distribute a cue's words inside its window by syllable + punctuation weight.

    The same weighting the estimator uses, but bounded to a *single cue's*
    ``[t_start, t_end]`` rather than the whole clip — so subtitle timing is honored
    cue-by-cue while still giving each word a sensible span.
    """
    tokens = tokenize(cue.text)
    span = cue.t_end - cue.t_start
    if not tokens or span <= 0.0:
        return []
    last = len(tokens) - 1
    weights = [word_weight(tok, gap_after=(i != last)) for i, tok in enumerate(tokens)]
    total = sum(weights) or float(len(tokens))
    out: list[WordTiming] = []
    cursor = cue.t_start
    for i, (tok, weight) in enumerate(zip(tokens, weights, strict=True)):
        portion = span * (weight / total)
        start = cursor
        end = cue.t_end if i == last else min(cue.t_end, start + portion)
        out.append(WordTiming(text=tok, t_start=round(start, 3), t_end=round(end, 3)))
        cursor = end
    return out


def _ingest_cues(cues: Sequence[RawCue | Mapping[str, Any]], *, unit: str) -> list[WordTiming]:
    out: list[WordTiming] = []
    for raw in cues:
        if isinstance(raw, RawCue):
            cue = raw
        else:
            start, end = _entry_times(raw, unit)
            cue = RawCue(text=_entry_text(raw), t_start=round(start, 3), t_end=round(end, 3))
        out.extend(words_from_cue(cue))
    return out


# --------------------------------------------------------------------------- #
# Shape sniffing
# --------------------------------------------------------------------------- #


def sniff_shape(entries: Sequence[Any] | None) -> TimingShape:
    """Best-effort guess of a raw timing payload's :class:`TimingShape`.

    Heuristics, in order: empty → ``NONE``; multi-word texts → ``CUE``; single
    characters → ``PER_CHAR``; word-piece / leading-space markers → ``PER_TOKEN``;
    otherwise ``PER_WORD``. The caller can always override by passing ``shape=``.
    """
    if not entries:
        return TimingShape.NONE
    texts = [_entry_text(e) for e in entries]
    nonblank = [t for t in texts if t.strip()]
    if not nonblank:
        return TimingShape.NONE
    # A phrase per entry (whitespace inside the text) → subtitle cues.
    if any(len(tokenize(t)) > 1 for t in nonblank):
        return TimingShape.CUE
    # Mostly single visible characters → per-char alignment. Checked *before* the
    # token-marker test because per-char streams use a bare " " entry as a word
    # separator, which would otherwise look like a leading-space token sentinel.
    visible = [t.strip() for t in nonblank]
    if visible and sum(len(t) == 1 for t in visible) / len(visible) > 0.6:
        return TimingShape.PER_CHAR
    # Explicit sub-word markers (SentencePiece ▁ / GPT-2 Ġ / ## / @@) → token stream.
    # A plain leading space is intentionally *not* a marker here (ambiguous with
    # per-char separators); per-token streams from such providers should pass shape=.
    if any(_CONTINUATION_RE.match(t) or t.startswith(("▁", "Ġ")) for t in texts):
        return TimingShape.PER_TOKEN
    return TimingShape.PER_WORD


# --------------------------------------------------------------------------- #
# The one entry point
# --------------------------------------------------------------------------- #


def ingest_timings(
    raw: Sequence[Any] | None,
    *,
    shape: TimingShape | None = None,
    text: str | None = None,
    duration_s: float | None = None,
    unit: str = "s",
) -> list[WordTiming]:
    """Normalize any provider timing payload into a canonical :class:`WordTiming` list.

    Args:
        raw: the provider's timing entries (mappings or timing-like objects), or
            ``None`` when the backend returned nothing.
        shape: the known :class:`TimingShape`; sniffed from ``raw`` when omitted.
        text: the spoken text — **required** to estimate timings when the resolved
            shape is ``NONE`` (no provider timings); ignored otherwise.
        duration_s: the measured clip/audio duration — required for the ``NONE``
            estimator path.
        unit: the time unit of ``raw`` (``"s"`` or ``"ms"``).

    Returns:
        Word timings on the audio clock. The ``NONE`` path marks every word
        ``estimated=True``; the others carry the provider's reported timing.

    Raises:
        ValueError: if the resolved shape is ``NONE`` (or ``raw`` is empty) but no
            ``text`` + positive ``duration_s`` was supplied to estimate from.
    """
    resolved = shape or sniff_shape(raw)
    if resolved is TimingShape.NONE or not raw:
        if not text or duration_s is None or duration_s <= 0.0:
            raise ValueError(
                "no provider timings and cannot estimate: "
                "pass text + a positive duration_s for the NONE shape"
            )
        return estimate_word_timings(text, duration_s=duration_s)

    if resolved is TimingShape.PER_WORD:
        return _ingest_per_word(raw, unit=unit)
    if resolved is TimingShape.PER_CHAR:
        return _ingest_per_char(raw, unit=unit)
    if resolved is TimingShape.PER_TOKEN:
        return _ingest_per_token(raw, unit=unit)
    if resolved is TimingShape.CUE:
        return _ingest_cues(raw, unit=unit)
    # Unreachable for the StrEnum, but keeps mypy exhaustive.
    raise ValueError(f"unsupported timing shape: {resolved!r}")  # pragma: no cover


__all__ = [
    "ingest_timings",
    "sniff_shape",
    "words_from_cue",
]
