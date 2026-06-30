"""Last-frame extraction for continuity (graceful, ffmpeg-optional).

Every open-model render returns its final frame so the continuity / FLF lane can
chain the next shot from where this one ended (§9.3 first-last-frame). Extraction
is a pure-bytes operation: decode the clip's last frame to a PNG with ffmpeg. It
is **best-effort** — when no ffmpeg binary is available (CI, minimal images) the
helper returns ``None`` rather than failing the render, exactly like the render
pipeline's enhancement lane.

Reuses :func:`app.render.degrade.get_ffmpeg_exe` so binary resolution
(``KINORA_FFMPEG`` > system > imageio bundle) stays identical across the codebase.
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from app.core.logging import get_logger

logger = get_logger("app.video.adapters.open.lastframe")

#: Hard cap on the extraction subprocess (a single seek+decode is fast).
_EXTRACT_TIMEOUT_S = 30.0


def ffmpeg_available() -> bool:
    """True when an ffmpeg binary can be resolved (delegates to the render lane)."""
    from app.render.degrade import ffmpeg_available as _avail

    return _avail()


def extract_last_frame(clip_bytes: bytes, *, fmt: str = "png") -> bytes | None:
    """Decode the final frame of ``clip_bytes`` to image bytes, or ``None``.

    Returns ``None`` (never raises) when ffmpeg is unavailable or the clip can't be
    decoded, so a continuity frame is a bonus rather than a render-failing
    dependency. ``-sseof -0.1`` seeks to just before the end and ``-vframes 1``
    grabs the last decodable frame; ``-update 1`` lets the single-image muxer
    write one file.
    """
    if not clip_bytes:
        return None
    try:
        from app.render.degrade import get_ffmpeg_exe

        ffmpeg = get_ffmpeg_exe()
    except Exception:  # noqa: BLE001 - no binary → graceful skip
        logger.debug("video.open.lastframe.no_ffmpeg")
        return None

    with tempfile.TemporaryDirectory(prefix="kinora-lastframe-") as tmp:
        in_path = Path(tmp) / "clip.mp4"
        out_path = Path(tmp) / f"last.{fmt}"
        in_path.write_bytes(clip_bytes)
        args = [
            ffmpeg,
            "-y",
            "-sseof",
            "-0.5",
            "-i",
            str(in_path),
            "-update",
            "1",
            "-vframes",
            "1",
            str(out_path),
        ]
        try:
            proc = subprocess.run(  # noqa: S603 - args built from a resolved binary
                args,
                capture_output=True,
                timeout=_EXTRACT_TIMEOUT_S,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            logger.debug("video.open.lastframe.subprocess_failed", error=str(exc))
            return None
        if proc.returncode != 0 or not out_path.exists():
            # A clip shorter than the seek window: retry without the seek so very
            # short clips still yield a final frame.
            retry = [
                ffmpeg,
                "-y",
                "-i",
                str(in_path),
                "-update",
                "1",
                "-vf",
                "select=eq(n\\,0)",
                "-vframes",
                "1",
                str(out_path),
            ]
            try:
                proc = subprocess.run(  # noqa: S603
                    retry, capture_output=True, timeout=_EXTRACT_TIMEOUT_S, check=False
                )
            except (OSError, subprocess.TimeoutExpired):
                return None
            if proc.returncode != 0 or not out_path.exists():
                logger.debug("video.open.lastframe.decode_failed")
                return None
        data = out_path.read_bytes()
        return data or None


__all__ = ["extract_last_frame", "ffmpeg_available"]
