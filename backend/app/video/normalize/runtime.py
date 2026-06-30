"""Binary resolution + a hardened subprocess runner for the normalize executors.

Mirrors :mod:`app.render.degrade`'s portable resolution (``KINORA_FFMPEG`` env >
system > the ``imageio-ffmpeg`` bundle) and its single hardened ``subprocess.run``
wrapper, so this subsystem invokes ffmpeg/ffprobe the exact same way the rest of
the repo does — and tests can skip cleanly when no binary is present.

Kept separate from the pure :mod:`app.video.normalize.plan` layer: nothing here
is imported by the plan builders, so the plan tests never touch a subprocess.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from app.core.logging import get_logger

logger = get_logger("app.video.normalize.runtime")

DEFAULT_TIMEOUT_S = 240.0


class NormalizeError(RuntimeError):
    """An ffmpeg/ffprobe invocation failed, timed out, or no binary is available."""


def get_ffmpeg_exe() -> str:
    """Resolve an ffmpeg binary: ``KINORA_FFMPEG`` > system > imageio bundle."""
    override = os.environ.get("KINORA_FFMPEG")
    if override and Path(override).exists():
        return override
    system = shutil.which("ffmpeg")
    if system:
        return system
    try:
        import imageio_ffmpeg

        bundled = imageio_ffmpeg.get_ffmpeg_exe()
        if bundled and Path(bundled).exists():
            return bundled
    except Exception:  # noqa: BLE001 - any import/resolve failure → fall through
        pass
    raise NormalizeError(
        "no ffmpeg binary available (set KINORA_FFMPEG, install ffmpeg, or imageio-ffmpeg)"
    )


def get_ffprobe_exe() -> str | None:
    """Resolve an ffprobe binary, or ``None`` (then probing decodes via ffmpeg)."""
    override = os.environ.get("KINORA_FFPROBE")
    if override and Path(override).exists():
        return override
    return shutil.which("ffprobe")


def ffmpeg_available() -> bool:
    """True when some ffmpeg binary can be resolved (system or bundled)."""
    try:
        get_ffmpeg_exe()
    except NormalizeError:
        return False
    return True


def ffprobe_available() -> bool:
    """True when an ffprobe binary can be resolved."""
    return get_ffprobe_exe() is not None


def run(
    args: list[str], *, timeout: float = DEFAULT_TIMEOUT_S
) -> subprocess.CompletedProcess[bytes]:
    """Run a resolved binary, raising :class:`NormalizeError` with a stderr tail."""
    try:
        proc = subprocess.run(  # noqa: S603 - args built from resolved binaries
            args,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:  # pragma: no cover - resolved just-in-time
        raise NormalizeError(f"binary not found: {args[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise NormalizeError(f"ffmpeg timed out after {timeout}s: {args[0]}") from exc
    if proc.returncode != 0:
        tail = proc.stderr.decode("utf-8", "replace")[-1500:]
        raise NormalizeError(f"{Path(args[0]).name} exited {proc.returncode}: {tail}")
    return proc


__all__ = [
    "DEFAULT_TIMEOUT_S",
    "NormalizeError",
    "ffmpeg_available",
    "ffprobe_available",
    "get_ffmpeg_exe",
    "get_ffprobe_exe",
    "run",
]
