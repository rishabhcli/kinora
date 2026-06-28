"""WEBVTT authoring — sprite-thumbnail cues + chapter cues (pure).

Two WEBVTT flavours the reading-room player consumes:

* **Sprite cues** — each cue spans an interval of the timeline and points at a
  rectangle of the sprite sheet via the ``#xywh=`` media-fragment syntax, so the
  player shows the right tile while the reader scrubs (one sheet, N cues, zero
  extra requests).
* **Chapter cues** — named time ranges (scene/beat boundaries) for a chapter
  rail.

Pure string authoring with no I/O or ffmpeg, so it is fully unit-testable and
deterministic. Timestamps use the WEBVTT ``HH:MM:SS.mmm`` form.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass


def format_timestamp(seconds: float) -> str:
    """Format ``seconds`` as a WEBVTT ``HH:MM:SS.mmm`` timestamp."""
    if seconds < 0:
        seconds = 0.0
    total_ms = int(round(seconds * 1000))
    ms = total_ms % 1000
    total_s = total_ms // 1000
    s = total_s % 60
    total_m = total_s // 60
    m = total_m % 60
    h = total_m // 60
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"


def sprite_vtt(
    *,
    sprite_url: str,
    columns: int,
    rows: int,
    tile_width: int,
    tile_height: int,
    tile_count: int,
    interval_s: float,
) -> str:
    """Author a sprite-thumbnail WEBVTT for a sheet of ``tile_count`` tiles.

    Each cue covers ``interval_s`` of the timeline and references the tile at the
    matching grid position via ``<sprite_url>#xywh=x,y,w,h``. Tiles are laid out
    left-to-right, top-to-bottom (the order :func:`app.media.images.build_sprite_sheet`
    produces).
    """
    if columns < 1 or rows < 1:
        raise ValueError("columns and rows must be >= 1")
    lines = ["WEBVTT", ""]
    for i in range(tile_count):
        start = i * interval_s
        end = (i + 1) * interval_s
        col = i % columns
        row = i // columns
        if row >= rows:  # defensive: never index past the sheet
            break
        x = col * tile_width
        y = row * tile_height
        lines.append(f"{format_timestamp(start)} --> {format_timestamp(end)}")
        lines.append(f"{sprite_url}#xywh={x},{y},{tile_width},{tile_height}")
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


@dataclass(frozen=True, slots=True)
class Chapter:
    """A named time range on the film timeline (scene/beat)."""

    start_s: float
    end_s: float
    title: str


def chapters_vtt(chapters: Sequence[Chapter]) -> str:
    """Author a chapter WEBVTT (named time ranges, in order).

    Chapters are emitted sorted by start time; overlapping/zero-length ranges are
    tolerated (the player decides). Each cue carries a numeric id (1-based).
    """
    ordered = sorted(chapters, key=lambda c: c.start_s)
    lines = ["WEBVTT", ""]
    for idx, ch in enumerate(ordered, start=1):
        lines.append(str(idx))
        lines.append(f"{format_timestamp(ch.start_s)} --> {format_timestamp(ch.end_s)}")
        lines.append(ch.title)
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


__all__ = [
    "Chapter",
    "chapters_vtt",
    "format_timestamp",
    "sprite_vtt",
]
