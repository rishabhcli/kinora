"""Poster / thumbnail / sprite-sheet generation (real ffmpeg).

Derivations the reading-room player needs from a generated clip/scene:

* **poster** — a single representative frame (PNG), used as the ``<video poster>``
  and the shelf still.
* **thumbnail** — a small, fixed-width still (PNG/JPEG) for the scrubber tooltip
  and library grid.
* **sprite sheet** — a grid of N evenly-spaced frames in one image, plus a
  sibling WEBVTT (authored by :mod:`app.media.vtt`) that maps a hover time to a
  tile rectangle so the player can show a preview while scrubbing without N
  separate requests.

Every function takes raw clip bytes and returns raw image bytes (no I/O), so
they are unit-testable against the tiny real mp4 from
:func:`app.media.testing.tiny_mp4`. ffmpeg is resolved via the existing portable
resolver in :mod:`app.render.degrade`.
"""

from __future__ import annotations

import math
import tempfile
from dataclasses import dataclass
from pathlib import Path

from app.core.logging import get_logger
from app.media.errors import PackagingError

logger = get_logger("app.media.images")

#: Default thumbnail width (px); height derives from the source aspect.
DEFAULT_THUMB_WIDTH = 320
#: Default sprite tile width (px) — small so a full sheet stays light.
DEFAULT_SPRITE_TILE_WIDTH = 160
#: Default number of sprite tiles (one row unless ``columns`` forces a grid).
DEFAULT_SPRITE_COUNT = 20


def _ffmpeg() -> str:
    from app.render.degrade import FfmpegError, get_ffmpeg_exe

    try:
        return get_ffmpeg_exe()
    except FfmpegError as exc:  # pragma: no cover - exercised via skip in tests
        raise PackagingError(str(exc)) from exc


def _run(args: list[str]) -> None:
    from app.render.degrade import FfmpegError, run_ffmpeg

    try:
        run_ffmpeg(args)
    except FfmpegError as exc:
        raise PackagingError(str(exc)) from exc


def extract_poster(
    clip_bytes: bytes,
    *,
    at_s: float | None = None,
    width: int | None = None,
) -> bytes:
    """Extract one PNG frame as the poster.

    ``at_s`` defaults to ~10% into the clip (a settled frame, not the often-black
    very first frame). ``width`` (if given) scales the still preserving aspect.
    """
    if not clip_bytes:
        raise PackagingError("clip_bytes is empty")
    ffmpeg = _ffmpeg()
    with tempfile.TemporaryDirectory(prefix="kinora_poster_") as tmp:
        src = Path(tmp) / "clip.mp4"
        src.write_bytes(clip_bytes)
        out = Path(tmp) / "poster.png"
        seek = _poster_seek(clip_bytes, at_s)
        vf = f"scale={width}:-2" if width else None
        args = [ffmpeg, "-y", "-ss", f"{seek:.3f}", "-i", str(src), "-frames:v", "1"]
        if vf:
            args += ["-vf", vf]
        args += ["-f", "image2", str(out)]
        _run(args)
        data = out.read_bytes()
    logger.info("media.poster", at_s=round(seek, 3), bytes=len(data))
    return data


def _poster_seek(clip_bytes: bytes, at_s: float | None) -> float:
    if at_s is not None:
        return max(0.0, at_s)
    try:
        from app.media.probe import probe_media

        dur = probe_media(clip_bytes).duration_s
        return max(0.0, dur * 0.1)
    except Exception:  # noqa: BLE001
        return 0.0


def extract_thumbnail(
    clip_bytes: bytes,
    *,
    at_s: float | None = None,
    width: int = DEFAULT_THUMB_WIDTH,
) -> bytes:
    """A small, fixed-width thumbnail still (PNG)."""
    return extract_poster(clip_bytes, at_s=at_s, width=width)


@dataclass(frozen=True, slots=True)
class SpriteSheet:
    """A generated sprite sheet + the geometry a WEBVTT author needs."""

    image: bytes
    columns: int
    rows: int
    tile_width: int
    tile_height: int
    tile_count: int
    interval_s: float
    duration_s: float

    @property
    def sheet_width(self) -> int:
        return self.columns * self.tile_width

    @property
    def sheet_height(self) -> int:
        return self.rows * self.tile_height


def build_sprite_sheet(
    clip_bytes: bytes,
    *,
    count: int = DEFAULT_SPRITE_COUNT,
    columns: int | None = None,
    tile_width: int = DEFAULT_SPRITE_TILE_WIDTH,
) -> SpriteSheet:
    """Tile ``count`` evenly-spaced frames into one PNG sprite sheet.

    The frames are sampled at ``duration / count`` intervals and laid out in a
    grid of ``columns`` (defaults to a near-square grid). Tile height derives
    from the source aspect so a vertical reel produces tall tiles. Returns a
    :class:`SpriteSheet` whose geometry feeds :func:`app.media.vtt.sprite_vtt`.
    """
    if not clip_bytes:
        raise PackagingError("clip_bytes is empty")
    if count < 1:
        raise PackagingError("sprite count must be >= 1")

    from app.media.probe import probe_media

    probe = probe_media(clip_bytes)
    duration = probe.duration_s or 1.0
    cols = columns or max(1, int(math.ceil(math.sqrt(count))))
    rows = max(1, int(math.ceil(count / cols)))
    interval = duration / count
    # frame rate that yields ~`count` frames over the clip
    fps = max(count / duration, 0.001)

    ffmpeg = _ffmpeg()
    with tempfile.TemporaryDirectory(prefix="kinora_sprite_") as tmp:
        src = Path(tmp) / "clip.mp4"
        src.write_bytes(clip_bytes)
        out = Path(tmp) / "sprite.png"
        # fps sampling → scale tile → tile into a cols×rows mosaic.
        vf = (
            f"fps={fps:.6f},"
            f"scale={tile_width}:-2,"
            f"tile={cols}x{rows}"
        )
        _run(
            [
                ffmpeg,
                "-y",
                "-i",
                str(src),
                "-frames:v",
                "1",
                "-vf",
                vf,
                "-f",
                "image2",
                str(out),
            ]
        )
        data = out.read_bytes()
        tile_h = _png_size(data)[1] // rows if data else 0

    sheet = SpriteSheet(
        image=data,
        columns=cols,
        rows=rows,
        tile_width=tile_width,
        tile_height=tile_h,
        tile_count=count,
        interval_s=interval,
        duration_s=duration,
    )
    logger.info(
        "media.sprite",
        count=count,
        grid=f"{cols}x{rows}",
        tile=f"{tile_width}x{tile_h}",
        bytes=len(data),
    )
    return sheet


def _png_size(data: bytes) -> tuple[int, int]:
    """Read width/height from a PNG header (IHDR), best-effort."""
    if len(data) >= 24 and data[:8] == b"\x89PNG\r\n\x1a\n":
        width = int.from_bytes(data[16:20], "big")
        height = int.from_bytes(data[20:24], "big")
        return width, height
    return (0, 0)


def png_size(data: bytes) -> tuple[int, int]:
    """Public PNG (width, height) reader (returns ``(0, 0)`` if not a PNG)."""
    return _png_size(data)


__all__ = [
    "DEFAULT_SPRITE_COUNT",
    "DEFAULT_SPRITE_TILE_WIDTH",
    "DEFAULT_THUMB_WIDTH",
    "SpriteSheet",
    "build_sprite_sheet",
    "extract_poster",
    "extract_thumbnail",
    "png_size",
]
