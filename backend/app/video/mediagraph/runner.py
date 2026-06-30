"""The injectable runner — the one seam between the pure plan and real ffmpeg.

The engine never spawns a subprocess itself; it hands each
:class:`~app.video.mediagraph.types.FfmpegInvocation` to a :class:`Runner`. This
keeps the scheduling / caching / failure-isolation logic fully unit-testable: a
:class:`FakeRunner` records the exact commands and returns canned results with no
ffmpeg, while :class:`SubprocessRunner` resolves and runs the real binaries
(ffmpeg-gated integration tests only).

Binary resolution is portable and self-owned (``KINORA_FFMPEG`` / ``KINORA_FFPROBE``
override → system ``ffmpeg``/``ffprobe`` → the ``imageio-ffmpeg`` bundled static
binary). It deliberately re-derives this locally rather than importing
``app.render.degrade`` so the subsystem stands alone under its own namespace.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Protocol, runtime_checkable

from app.core.logging import get_logger
from app.video.mediagraph.types import FfmpegInvocation, RunResult

logger = get_logger("app.video.mediagraph.runner")

#: Default per-invocation wall-clock ceiling (seconds).
DEFAULT_TIMEOUT_S = 240.0


class BinaryUnavailableError(RuntimeError):
    """No ffmpeg/ffprobe binary could be resolved for the requested logical name."""


# --------------------------------------------------------------------------- #
# Portable binary resolution (self-owned; mirrors degrade.py's policy)
# --------------------------------------------------------------------------- #


def resolve_ffmpeg() -> str:
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
    except Exception:  # noqa: BLE001 - any resolve failure → unavailable
        pass
    raise BinaryUnavailableError(
        "no ffmpeg binary (set KINORA_FFMPEG, install ffmpeg, or imageio-ffmpeg)"
    )


def resolve_ffprobe() -> str | None:
    """Resolve an ffprobe binary, or ``None`` if none is installed."""
    override = os.environ.get("KINORA_FFPROBE")
    if override and Path(override).exists():
        return override
    return shutil.which("ffprobe")


def ffmpeg_available() -> bool:
    """True when some ffmpeg binary can be resolved (system or bundled)."""
    try:
        resolve_ffmpeg()
    except BinaryUnavailableError:
        return False
    return True


def _resolve(binary: str) -> str:
    """Map a logical binary name to a concrete executable path."""
    if binary == "ffprobe":
        probe = resolve_ffprobe()
        if probe is None:
            raise BinaryUnavailableError("no ffprobe binary available")
        return probe
    if binary == "ffmpeg":
        return resolve_ffmpeg()
    # Any other logical name must already be a resolvable path / on PATH.
    found = shutil.which(binary)
    if found:
        return found
    if Path(binary).exists():
        return binary
    raise BinaryUnavailableError(f"cannot resolve binary {binary!r}")


# --------------------------------------------------------------------------- #
# Runner protocol
# --------------------------------------------------------------------------- #


@runtime_checkable
class Runner(Protocol):
    """Executes one invocation and returns its :class:`RunResult` (async)."""

    async def run(self, invocation: FfmpegInvocation) -> RunResult: ...


# --------------------------------------------------------------------------- #
# Real subprocess runner
# --------------------------------------------------------------------------- #


class SubprocessRunner:
    """Runs a real ffmpeg/ffprobe subprocess (off the event loop thread)."""

    def __init__(self, *, timeout_s: float = DEFAULT_TIMEOUT_S) -> None:
        self.timeout_s = timeout_s

    async def run(self, invocation: FfmpegInvocation) -> RunResult:
        return await asyncio.to_thread(self._run_sync, invocation)

    def _run_sync(self, invocation: FfmpegInvocation) -> RunResult:
        try:
            binary = _resolve(invocation.binary)
        except BinaryUnavailableError as exc:
            logger.warning(
                "mediagraph.runner.unavailable", binary=invocation.binary, error=str(exc)
            )
            return RunResult(ok=False, stderr=str(exc), returncode=127)
        args = [binary, *invocation.args]
        started = time.monotonic()
        try:
            proc = subprocess.run(  # noqa: S603 - binary resolved, args are planned data
                args,
                capture_output=True,
                timeout=self.timeout_s,
                check=False,
            )
        except FileNotFoundError as exc:  # pragma: no cover - resolved just-in-time
            return RunResult(ok=False, stderr=f"binary not found: {binary}: {exc}", returncode=127)
        except subprocess.TimeoutExpired:
            return RunResult(
                ok=False,
                stderr=f"timed out after {self.timeout_s}s: {invocation.label or binary}",
                returncode=124,
                duration_s=time.monotonic() - started,
            )
        elapsed = time.monotonic() - started
        stdout = proc.stdout.decode("utf-8", "replace")
        stderr = proc.stderr.decode("utf-8", "replace")
        ok = proc.returncode == 0
        if not ok:
            logger.warning(
                "mediagraph.runner.failed",
                label=invocation.label,
                returncode=proc.returncode,
                stderr_tail=stderr[-600:],
            )
        return RunResult(
            ok=ok,
            stdout=stdout,
            stderr=stderr[-2000:],
            returncode=proc.returncode,
            duration_s=elapsed,
        )


# --------------------------------------------------------------------------- #
# Fake runner (deterministic, no ffmpeg)
# --------------------------------------------------------------------------- #


class FakeRunner:
    """A deterministic, ffmpeg-free runner for unit tests.

    Records every invocation it is handed (in order), optionally writes a small
    placeholder file at each invocation's declared output path (so downstream
    nodes see a real file to hash), and returns canned results. A set of labels /
    a predicate can be marked to *fail* so failure-isolation paths are exercised
    without a real ffmpeg error.
    """

    def __init__(
        self,
        *,
        fail_labels: set[str] | None = None,
        write_outputs: bool = True,
        probe_stdout: str = "{}",
        placeholder: bytes = b"FAKE_MEDIA",
    ) -> None:
        self.calls: list[FfmpegInvocation] = []
        self.commands: list[tuple[str, ...]] = []
        self._fail_labels = fail_labels or set()
        self._write_outputs = write_outputs
        self._probe_stdout = probe_stdout
        self._placeholder = placeholder

    async def run(self, invocation: FfmpegInvocation) -> RunResult:
        self.calls.append(invocation)
        self.commands.append(invocation.command())
        if invocation.label in self._fail_labels:
            return RunResult(ok=False, stderr=f"forced failure: {invocation.label}", returncode=1)
        # Materialise the declared output so downstream hashing/reads work.
        if self._write_outputs and not invocation.captures_stdout:
            out_path = _output_path_of(invocation)
            if out_path is not None:
                out_path.parent.mkdir(parents=True, exist_ok=True)
                # Make the bytes depend on the label so distinct outputs differ.
                payload = self._placeholder + b"::" + (invocation.label or "").encode()
                out_path.write_bytes(payload)
        stdout = self._probe_stdout if invocation.captures_stdout else ""
        return RunResult(ok=True, stdout=stdout, returncode=0)


def _output_path_of(invocation: FfmpegInvocation) -> Path | None:
    """Best-effort: the last path-like token in an invocation's args (its output).

    ffmpeg's output file is conventionally the final positional argument. The fake
    runner uses this to drop a placeholder so downstream nodes have a file to read,
    without the fake needing the planner's resolved paths.
    """
    media_exts = (".mp4", ".png", ".jpg", ".gif", ".m4a", ".json")
    for token in reversed(invocation.args):
        if "/" in token or "\\" in token or token.endswith(media_exts):
            return Path(token)
    return None


__all__ = [
    "DEFAULT_TIMEOUT_S",
    "BinaryUnavailableError",
    "FakeRunner",
    "Runner",
    "SubprocessRunner",
    "ffmpeg_available",
    "resolve_ffmpeg",
    "resolve_ffprobe",
]
