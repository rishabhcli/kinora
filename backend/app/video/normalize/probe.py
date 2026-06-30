"""``ClipProbe`` — the ffprobe wrapper that yields a typed :class:`MediaInfo`.

Bridges raw bytes / a file path to the pure :func:`parse_ffprobe_json` parser:
runs ``ffprobe -of json -show_format -show_streams`` and hands its stdout to the
parser. When no ffprobe binary exists (the portable ``imageio-ffmpeg``-only
image), it falls back to parsing ``ffmpeg -i`` stderr for the salient facts, so a
probe never hard-fails just because ffprobe is missing — the same resilience
:mod:`app.render.degrade` provides.
"""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
from pathlib import Path

from app.core.logging import get_logger

from .media_info import MediaInfo, StreamInfo, parse_ffprobe_json, parse_rational
from .runtime import NormalizeError, get_ffmpeg_exe, get_ffprobe_exe, run

logger = get_logger("app.video.normalize.probe")

#: ``Duration: HH:MM:SS.ss`` as ffmpeg prints it to stderr on an ``-i`` probe.
_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
_VIDEO_RE = re.compile(
    r"Stream #\S+:\s*Video:\s*(\w+).*?(\d{2,5})x(\d{2,5})(?:.*?\b([\d.]+)\s*fps)?",
    re.DOTALL,
)
_AUDIO_RE = re.compile(
    r"Stream #\S+:\s*Audio:\s*(\w+).*?(\d+)\s*Hz(?:.*?(mono|stereo|\d+ channels))?"
)


class ClipProbe:
    """Probe a clip (bytes or path) into a typed :class:`MediaInfo`.

    Stateless and cheap to construct; ``timeout_s`` bounds each ffprobe/ffmpeg
    invocation. Prefer :meth:`probe_bytes` for in-memory provider clips and
    :meth:`probe_path` when the clip already lives on disk (avoids a copy).
    """

    def __init__(self, *, timeout_s: float = 60.0) -> None:
        self._timeout = timeout_s

    def available(self) -> bool:
        """True when ffprobe is resolvable (else :meth:`probe_*` uses the fallback)."""
        return get_ffprobe_exe() is not None

    def probe_bytes(self, data: bytes) -> MediaInfo:
        """Probe in-memory clip bytes (written to a temp file first)."""
        if not data:
            return MediaInfo()
        with tempfile.TemporaryDirectory(prefix="kinora_probe_") as tmp:
            path = Path(tmp) / "clip"
            path.write_bytes(data)
            return self.probe_path(str(path))

    def probe_path(self, path: str) -> MediaInfo:
        """Probe a clip already on disk, preferring ffprobe with an ffmpeg fallback."""
        ffprobe = get_ffprobe_exe()
        if ffprobe is None:
            return self._probe_via_ffmpeg(path)
        proc = run(
            [
                ffprobe,
                "-v",
                "error",
                "-show_format",
                "-show_streams",
                "-of",
                "json",
                path,
            ],
            timeout=self._timeout,
        )
        payload = json.loads(proc.stdout.decode("utf-8", "replace") or "{}")
        return parse_ffprobe_json(payload)

    # -- ffprobe-free fallback ------------------------------------------- #

    def _probe_via_ffmpeg(self, path: str) -> MediaInfo:
        """Parse ``ffmpeg -i`` stderr for duration / geometry / codecs / audio.

        Best-effort, mirroring :func:`app.render.degrade._probe_via_ffmpeg`: the
        portable image ships only the imageio ffmpeg binary, so the normalize path
        must still learn each clip's shape. Unknown fields stay ``None``/0.
        """
        ffmpeg = get_ffmpeg_exe()
        proc = subprocess.run(  # noqa: S603 - binary resolved, args fixed
            [ffmpeg, "-hide_banner", "-i", path],
            capture_output=True,
            timeout=self._timeout,
            check=False,
        )
        err = proc.stderr.decode("utf-8", "replace")
        duration = 0.0
        if (m := _DURATION_RE.search(err)) is not None:
            h, mnt, sec = m.groups()
            duration = int(h) * 3600 + int(mnt) * 60 + float(sec)

        streams: list[StreamInfo] = []
        if (v := _VIDEO_RE.search(err)) is not None:
            fps = parse_rational(v.group(4)) if v.group(4) else None
            streams.append(
                StreamInfo(
                    index=0,
                    codec_type="video",
                    codec_name=v.group(1),
                    width=int(v.group(2)),
                    height=int(v.group(3)),
                    fps=fps,
                )
            )
        if (a := _AUDIO_RE.search(err)) is not None:
            layout = a.group(3)
            channels = {"mono": 1, "stereo": 2}.get(layout or "")
            if channels is None and layout and "channels" in layout:
                channels = int(layout.split()[0])
            streams.append(
                StreamInfo(
                    index=len(streams),
                    codec_type="audio",
                    codec_name=a.group(1),
                    sample_rate=int(a.group(2)),
                    channels=channels,
                    channel_layout=layout,
                )
            )
        return MediaInfo(container="ffmpeg-stderr", duration_s=duration, streams=streams)

    @staticmethod
    def is_error(exc: BaseException) -> bool:
        """Whether ``exc`` is a normalize-subsystem ffmpeg error (for callers)."""
        return isinstance(exc, NormalizeError)


__all__ = ["ClipProbe"]
