"""Universal last-frame extraction — the image-to-video continuation handoff.

The §9.3 continuation modes (``video_continuation`` / first-last-frame) seed the
next clip from the *final* frame of the accepted one, so a long event flows as a
single unbroken take. Provider clips arrive in any container (mp4 / webm / mov,
CFR or VFR, odd GOP), so a robust extractor must not assume a seekable keyframe at
the tail. :func:`extract_last_frame` runs the fast ``-sseof`` recipe and silently
falls back to a full-decode "keep the last written frame" pass for the containers
that report no usable duration.

Returns raw image bytes (PNG by default) — the render worker persists them to
object storage and hands the signed URL to the i2v provider as ``first_frame_url``.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import anyio

from app.core.logging import get_logger

from .plan import build_last_frame_args, build_last_frame_fallback_args
from .probe import ClipProbe
from .runtime import NormalizeError, get_ffmpeg_exe, run

logger = get_logger("app.video.normalize.lastframe")

#: Supported still formats → file extension (selects ffmpeg's image muxer).
_FORMAT_EXT = {"png": ".png", "jpg": ".jpg", "jpeg": ".jpg"}


def extract_last_frame(
    data: bytes,
    *,
    image_format: str = "png",
    duration_s: float | None = None,
    timeout_s: float = 60.0,
) -> bytes:
    """Extract the final decodable frame of ``data`` as a still image (any container).

    Args:
        data: the source clip bytes.
        image_format: ``"png"`` (lossless, default) or ``"jpg"``.
        duration_s: the clip length when already known (skips a probe and enables
            the fast end-seek); ``None`` triggers a quick internal probe.
        timeout_s: per-invocation ffmpeg ceiling.

    Returns:
        The last frame's image bytes.

    Raises:
        ValueError: when ``data`` is empty or ``image_format`` is unsupported.
        NormalizeError: when no ffmpeg binary is available or both passes fail.
    """
    if not data:
        raise ValueError("extract_last_frame requires non-empty clip data")
    ext = _FORMAT_EXT.get(image_format.lower())
    if ext is None:
        raise ValueError(f"unsupported image_format: {image_format!r}")

    ffmpeg = get_ffmpeg_exe()
    if duration_s is None:
        try:
            duration_s = ClipProbe(timeout_s=min(timeout_s, 30.0)).probe_bytes(data).duration_s
        except NormalizeError:
            duration_s = None

    with tempfile.TemporaryDirectory(prefix="kinora_lastframe_") as tmp:
        tmp_dir = Path(tmp)
        in_path = tmp_dir / "clip"
        in_path.write_bytes(data)
        out_path = tmp_dir / f"last{ext}"

        # Fast path: end-seek then keep the final frame.
        fast = build_last_frame_args(
            ffmpeg=ffmpeg,
            in_path=str(in_path),
            out_path=str(out_path),
            duration_s=duration_s,
        )
        try:
            run(fast, timeout=timeout_s)
        except NormalizeError:
            out_path.unlink(missing_ok=True)

        if not out_path.exists() or out_path.stat().st_size == 0:
            # Robust fallback: full decode, keep the last written frame.
            out_path.unlink(missing_ok=True)
            fallback = build_last_frame_fallback_args(
                ffmpeg=ffmpeg, in_path=str(in_path), out_path=str(out_path)
            )
            run(fallback, timeout=timeout_s)

        if not out_path.exists() or out_path.stat().st_size == 0:
            raise NormalizeError("last-frame extraction produced no image")
        frame = out_path.read_bytes()

    logger.info("normalize.last_frame", format=image_format, bytes=len(frame))
    return frame


async def extract_last_frame_async(
    data: bytes,
    *,
    image_format: str = "png",
    duration_s: float | None = None,
    timeout_s: float = 60.0,
) -> bytes:
    """Async wrapper running the blocking extraction on a worker thread."""
    return await anyio.to_thread.run_sync(
        lambda: extract_last_frame(
            data, image_format=image_format, duration_s=duration_s, timeout_s=timeout_s
        )
    )


__all__ = ["extract_last_frame", "extract_last_frame_async"]
