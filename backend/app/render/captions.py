"""Caption export — turn the sync map into WebVTT / SRT cues (§3 accessibility).

Kinora's accessibility story (ADHD, dyslexia, ESL — §3) is stronger when the scene
ships *captions* a player can render natively, not just the in-app karaoke highlight.
The sync map already binds every narrated word to a time; this module groups those
words into readable caption cues and serialises them to the two standard subtitle
formats. A player loads the VTT track; a download offers the SRT.

Pure and deterministic: words in → cue text out, no model, no I/O. Cues are built by
packing words up to a max line length / max duration and breaking on the segment's
strong pauses (a sentence-ending word forces a cue boundary), so a cue is a readable
phrase rather than a fixed word count.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.render.sync_map import SyncSegment, SyncWord

#: Default cue packing limits — tuned for a vertical phone reel's caption band.
_MAX_CHARS = 42
_MAX_WORDS = 9
_MAX_CUE_S = 6.0
#: Word endings that force a cue break (sentence/clause boundaries read cleaner).
_HARD_BREAK = (".", "!", "?", "…")
_SOFT_BREAK = (",", ";", ":", "—")


@dataclass(frozen=True, slots=True)
class CaptionCue:
    """One subtitle cue: a phrase shown from ``t_start`` to ``t_end``."""

    index: int
    t_start: float
    t_end: float
    text: str


@dataclass
class _Builder:
    """Accumulates words into the current cue until a limit / break fires."""

    words: list[SyncWord] = field(default_factory=list)

    @property
    def char_len(self) -> int:
        return sum(len(w.text) + 1 for w in self.words)

    def would_overflow(self, word: SyncWord) -> bool:
        if not self.words:
            return False
        if len(self.words) >= _MAX_WORDS:
            return True
        if self.char_len + len(word.text) + 1 > _MAX_CHARS:
            return True
        return word.t_end - self.words[0].t_start > _MAX_CUE_S

    def flush(self, index: int) -> CaptionCue | None:
        if not self.words:
            return None
        cue = CaptionCue(
            index=index,
            t_start=round(self.words[0].t_start, 3),
            t_end=round(self.words[-1].t_end, 3),
            text=" ".join(w.text for w in self.words).strip(),
        )
        self.words = []
        return cue


def _ends_hard(word: SyncWord) -> bool:
    return word.text.rstrip("\"'”’)").endswith(_HARD_BREAK)


def build_cues(segments: list[SyncSegment]) -> list[CaptionCue]:
    """Pack the sync segments' words into readable caption cues (pure).

    Words flow across segment boundaries (a sentence may span shots), but a cue
    always breaks at a sentence end and never exceeds the char/word/duration limits.
    """
    cues: list[CaptionCue] = []
    builder = _Builder()
    for segment in segments:
        for word in segment.words:
            if builder.would_overflow(word):
                cue = builder.flush(len(cues) + 1)
                if cue is not None:
                    cues.append(cue)
            builder.words.append(word)
            if _ends_hard(word):
                cue = builder.flush(len(cues) + 1)
                if cue is not None:
                    cues.append(cue)
    final = builder.flush(len(cues) + 1)
    if final is not None:
        cues.append(final)
    return cues


def _ts(seconds: float, *, sep: str) -> str:
    """Format ``seconds`` as ``HH:MM:SS<sep>mmm`` (``sep`` = ``.`` VTT / ``,`` SRT)."""
    seconds = max(0.0, seconds)
    total_ms = int(round(seconds * 1000))
    hours, rem = divmod(total_ms, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    secs, ms = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}{sep}{ms:03d}"


def to_webvtt(cues: list[CaptionCue]) -> str:
    """Serialise cues to a WebVTT document (the in-player caption track)."""
    lines = ["WEBVTT", ""]
    for cue in cues:
        lines.append(str(cue.index))
        lines.append(f"{_ts(cue.t_start, sep='.')} --> {_ts(cue.t_end, sep='.')}")
        lines.append(cue.text)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def to_srt(cues: list[CaptionCue]) -> str:
    """Serialise cues to a SubRip (SRT) document (the downloadable subtitle)."""
    blocks: list[str] = []
    for cue in cues:
        blocks.append(
            f"{cue.index}\n"
            f"{_ts(cue.t_start, sep=',')} --> {_ts(cue.t_end, sep=',')}\n"
            f"{cue.text}"
        )
    return "\n\n".join(blocks) + ("\n" if blocks else "")


def segments_to_webvtt(segments: list[SyncSegment]) -> str:
    """Build cues from segments and serialise to WebVTT in one call."""
    return to_webvtt(build_cues(segments))


def segments_to_srt(segments: list[SyncSegment]) -> str:
    """Build cues from segments and serialise to SRT in one call."""
    return to_srt(build_cues(segments))


__all__ = [
    "CaptionCue",
    "build_cues",
    "segments_to_srt",
    "segments_to_webvtt",
    "to_srt",
    "to_webvtt",
]
