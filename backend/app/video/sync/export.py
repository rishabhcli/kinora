"""Export a sync map to WebVTT / SRT subtitle formats (§9.4 interop).

The §9.4 sync map is the karaoke source of truth, but a clip is also shippable to
players, accessibility layers, and review tools that speak the universal subtitle
formats. These exporters render a :class:`SyncMap` (or a bare word timeline) as:

* **WebVTT** — the web-native ``<track>`` format; one cue per sentence anchor, with
  optional per-word ``<00:00.000>`` inline timestamps so a compliant player can do
  word-level karaoke straight from the track.
* **SRT** — the lowest-common-denominator subtitle format; one cue per sentence
  (or per word), no inline word marks.

Timestamps are emitted at millisecond precision in each format's required syntax
(VTT uses ``.``, SRT uses ``,`` as the fractional separator). Pure string building.
"""

from __future__ import annotations

from collections.abc import Sequence

from .models import SyncMap, SyncWord


def _fmt_ts(seconds: float, *, sep: str) -> str:
    """Format ``seconds`` as ``HH:MM:SS<sep>mmm`` (VTT ``.``, SRT ``,``)."""
    total_ms = max(0, round(seconds * 1000))
    ms = total_ms % 1000
    total_s = total_ms // 1000
    s = total_s % 60
    m = (total_s // 60) % 60
    h = total_s // 3600
    return f"{h:02d}:{m:02d}:{s:02d}{sep}{ms:03d}"


def _word_cues(words: Sequence[SyncWord]) -> list[tuple[float, float, str]]:
    """One ``(start, end, text)`` cue per word."""
    return [(w.t_start, w.t_end, w.text) for w in words]


def _sentence_cues(sync_map: SyncMap) -> list[tuple[float, float, str]]:
    """One cue per sentence anchor; falls back to per-word when none exist."""
    if not sync_map.sentences:
        return _word_cues(sync_map.words)
    return [(s.t_start, s.t_end, s.text) for s in sync_map.sentences]


def to_webvtt(
    sync_map: SyncMap,
    *,
    per_word: bool = False,
    inline_word_timings: bool = True,
) -> str:
    """Render ``sync_map`` as a WebVTT document.

    Args:
        per_word: emit one cue per word instead of one per sentence.
        inline_word_timings: when grouping by sentence, embed per-word
            ``<HH:MM:SS.mmm>`` marks inside each cue so a compliant player can paint
            word-level karaoke from the track. Ignored in ``per_word`` mode.

    Returns a string beginning with the ``WEBVTT`` header.
    """
    lines = ["WEBVTT", ""]
    if per_word or not sync_map.sentences:
        for start, end, text in _word_cues(sync_map.words):
            lines.append(f"{_fmt_ts(start, sep='.')} --> {_fmt_ts(end, sep='.')}")
            lines.append(text)
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    for sent in sync_map.sentences:
        lines.append(f"{_fmt_ts(sent.t_start, sep='.')} --> {_fmt_ts(sent.t_end, sep='.')}")
        chunk = sync_map.words[sent.word_start : sent.word_end + 1]
        if inline_word_timings and chunk:
            payload = " ".join(f"<{_fmt_ts(w.t_start, sep='.')}>{w.text}" for w in chunk)
            lines.append(payload)
        else:
            lines.append(sent.text)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def to_srt(sync_map: SyncMap, *, per_word: bool = False) -> str:
    """Render ``sync_map`` as an SRT document (1-indexed cues, ``,`` separator)."""
    cues = _word_cues(sync_map.words) if per_word else _sentence_cues(sync_map)
    blocks: list[str] = []
    for idx, (start, end, text) in enumerate(cues, start=1):
        blocks.append(f"{idx}\n{_fmt_ts(start, sep=',')} --> {_fmt_ts(end, sep=',')}\n{text}\n")
    return "\n".join(blocks)


__all__ = ["to_srt", "to_webvtt"]
