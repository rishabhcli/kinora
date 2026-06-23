"""The degradation ladder — REAL ffmpeg, a genuine product feature (§4.4, §12.4).

The film never hard-stops. Under the live-video gate, low budget, or exhausted
retries, the per-shot pipeline steps *down* the ladder and silently steps back
up when pressure clears:

    full Wan video
      → generated/locked keyframe + Ken-Burns pan      (this module)
      → the book's own page illustration + Ken-Burns    (this module)
      → audio + highlighted text (a minimal narrated card)  (this module)

Every rung here produces a **real, playable mp4** with ffmpeg — the Ken-Burns
pan is a slow zoom/drift over a still, muxed with the narration audio. This is
the committed degradation rung the spec promises (§4.4): "indistinguishable
enough from a slow establishing shot to hold the moment", not a fake fallback.

ffmpeg is resolved portably: a ``KINORA_FFMPEG`` override, then a system
``ffmpeg``, then the ``imageio-ffmpeg`` bundled static binary — so the pipeline
does not *rely* on a system install. ``ffprobe`` (used for verification and
duration probing) is resolved similarly; when no ffprobe exists, validity is
confirmed by a decode pass through ffmpeg instead.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from app.core.logging import get_logger

logger = get_logger("app.render.degrade")

#: Default render geometry/timing for a degradation clip ("1080p-ish, sane fps").
DEFAULT_SIZE: tuple[int, int] = (1920, 1080)
DEFAULT_FPS: int = 30
#: Slow Ken-Burns zoom ceiling over the shot (1.0 → ZOOM_MAX across its length).
DEFAULT_ZOOM_MAX: float = 1.18
_FFMPEG_TIMEOUT_S = 180.0


class DegradeRung(StrEnum):
    """The §12.4 rungs below full Wan video (each cheaper than the last)."""

    #: Ken-Burns over a generated / locked / speculative keyframe still.
    KEN_BURNS_KEYFRAME = "ken_burns_keyframe"
    #: Ken-Burns over the book's own page illustration.
    KEN_BURNS_ILLUSTRATION = "ken_burns_illustration"
    #: The bottom rung: narrated audio + (client-painted) highlighted text.
    AUDIO_TEXT_ONLY = "audio_text_only"


class FfmpegError(RuntimeError):
    """An ffmpeg/ffprobe invocation failed or no binary is available."""


# --------------------------------------------------------------------------- #
# Binary resolution (portable: env > system > imageio-ffmpeg bundle)
# --------------------------------------------------------------------------- #


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
    raise FfmpegError(
        "no ffmpeg binary available (set KINORA_FFMPEG, install ffmpeg, or imageio-ffmpeg)"
    )


def get_ffprobe_exe() -> str | None:
    """Resolve an ffprobe binary, or ``None`` (then verification decodes instead)."""
    override = os.environ.get("KINORA_FFPROBE")
    if override and Path(override).exists():
        return override
    return shutil.which("ffprobe")


def ffmpeg_available() -> bool:
    """True when some ffmpeg binary can be resolved (system or bundled)."""
    try:
        get_ffmpeg_exe()
    except FfmpegError:
        return False
    return True


def _run(
    args: list[str], *, timeout: float = _FFMPEG_TIMEOUT_S
) -> subprocess.CompletedProcess[bytes]:
    """Run a binary, raising :class:`FfmpegError` with stderr tail on failure."""
    try:
        proc = subprocess.run(  # noqa: S603 - args are built from resolved binaries
            args,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:  # pragma: no cover - resolved just-in-time
        raise FfmpegError(f"binary not found: {args[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise FfmpegError(f"ffmpeg timed out after {timeout}s: {args[0]}") from exc
    if proc.returncode != 0:
        tail = proc.stderr.decode("utf-8", "replace")[-1200:]
        raise FfmpegError(f"{Path(args[0]).name} exited {proc.returncode}: {tail}")
    return proc


#: Public alias so sibling modules (e.g. ``stitch``) reuse one hardened runner.
run_ffmpeg = _run


# --------------------------------------------------------------------------- #
# Probing & verification (proof the artifact is real and playable)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ProbeInfo:
    """The salient ffprobe facts about a produced clip."""

    duration_s: float
    has_video: bool
    has_audio: bool
    width: int | None
    height: int | None
    video_codec: str | None
    audio_codec: str | None
    nb_streams: int
    raw: dict[str, object]


def probe(data: bytes) -> ProbeInfo:
    """Probe an mp4 with ffprobe (raw ``format``+``streams`` JSON parsed).

    Raises:
        FfmpegError: when no ffprobe is available — use :func:`verify_playable`
            for a binary validity check that needs only ffmpeg.
    """
    ffprobe = get_ffprobe_exe()
    if ffprobe is None:
        raise FfmpegError("ffprobe not available; use verify_playable() instead")
    with tempfile.TemporaryDirectory(prefix="kinora_probe_") as tmp:
        path = Path(tmp) / "clip.mp4"
        path.write_bytes(data)
        proc = _run(
            [
                ffprobe,
                "-v",
                "error",
                "-show_format",
                "-show_streams",
                "-of",
                "json",
                str(path),
            ]
        )
    payload = json.loads(proc.stdout.decode("utf-8", "replace") or "{}")
    streams = payload.get("streams", []) or []
    fmt = payload.get("format", {}) or {}
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    duration_raw = fmt.get("duration") or (video or {}).get("duration") or 0.0
    return ProbeInfo(
        duration_s=float(duration_raw),
        has_video=video is not None,
        has_audio=audio is not None,
        width=int(video["width"]) if video and "width" in video else None,
        height=int(video["height"]) if video and "height" in video else None,
        video_codec=video.get("codec_name") if video else None,
        audio_codec=audio.get("codec_name") if audio else None,
        nb_streams=len(streams),
        raw=payload,
    )


#: ``Duration: HH:MM:SS.ss`` as ffmpeg prints it to stderr on an ``-i`` probe.
_FFMPEG_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
#: ``Stream #..: Video: <codec> ..., <W>x<H>`` — grab codec + the first WxH.
_FFMPEG_VIDEO_RE = re.compile(r"Stream #\S+:\s*Video:\s*(\w+).*?(\d{2,5})x(\d{2,5})", re.DOTALL)
_FFMPEG_AUDIO_RE = re.compile(r"Stream #\S+:\s*Audio:\s*(\w+)")


def _probe_via_ffmpeg(data: bytes) -> ProbeInfo:
    """ffprobe-free fallback: parse ``ffmpeg -i`` stderr for the salient facts.

    The portable image ships only the ``imageio-ffmpeg`` binary (no ``ffprobe``),
    so the stitch path must still learn each clip's duration / geometry / audio
    presence. ``ffmpeg -i`` with no output exits non-zero but prints the stream
    table to stderr; we parse it. Best-effort — unknown fields stay ``None``/0.
    """
    if not data:
        return ProbeInfo(0.0, False, False, None, None, None, None, 0, {})
    ffmpeg = get_ffmpeg_exe()
    with tempfile.TemporaryDirectory(prefix="kinora_ffinfo_") as tmp:
        path = Path(tmp) / "clip.mp4"
        path.write_bytes(data)
        # No output file → ffmpeg exits 1 after printing the input's stream table.
        proc = subprocess.run(  # noqa: S603 - binary is resolved, args are fixed
            [ffmpeg, "-hide_banner", "-i", str(path)],
            capture_output=True,
            timeout=_FFMPEG_TIMEOUT_S,
            check=False,
        )
    err = proc.stderr.decode("utf-8", "replace")
    duration = 0.0
    if (m := _FFMPEG_DURATION_RE.search(err)) is not None:
        h, mnt, sec = m.groups()
        duration = int(h) * 3600 + int(mnt) * 60 + float(sec)
    video = _FFMPEG_VIDEO_RE.search(err)
    audio = _FFMPEG_AUDIO_RE.search(err)
    width = int(video.group(2)) if video else None
    height = int(video.group(3)) if video else None
    nb_streams = len(re.findall(r"Stream #\S+:", err))
    return ProbeInfo(
        duration_s=duration,
        has_video=video is not None,
        has_audio=audio is not None,
        width=width,
        height=height,
        video_codec=video.group(1) if video else None,
        audio_codec=audio.group(1) if audio else None,
        nb_streams=nb_streams,
        raw={"source": "ffmpeg-stderr"},
    )


def inspect(data: bytes) -> ProbeInfo:
    """Probe a clip, preferring ffprobe but falling back to ``ffmpeg -i`` stderr.

    This is the portable entry point siblings (e.g. :mod:`app.render.stitch`)
    should use: it returns the same :class:`ProbeInfo` whether or not an
    ``ffprobe`` binary is installed, so the stitch/concat path is correct on the
    bundled-``ffmpeg``-only image (where :func:`probe` would raise).
    """
    if get_ffprobe_exe() is not None:
        return probe(data)
    return _probe_via_ffmpeg(data)


def verify_playable(data: bytes) -> bool:
    """Decode the whole clip through ffmpeg to confirm it is valid + playable.

    Independent of ffprobe (uses only ffmpeg), so it works on the portable path.
    """
    if not data:
        return False
    with tempfile.TemporaryDirectory(prefix="kinora_verify_") as tmp:
        path = Path(tmp) / "clip.mp4"
        path.write_bytes(data)
        try:
            _run([get_ffmpeg_exe(), "-v", "error", "-i", str(path), "-f", "null", "-"])
        except FfmpegError:
            return False
    return True


# --------------------------------------------------------------------------- #
# Image helpers
# --------------------------------------------------------------------------- #


def _sniff_ext(raw: bytes) -> str:
    if raw[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return ".webp"
    if raw[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    return ".png"


def _frames_for(duration_s: float, fps: int) -> int:
    return max(1, round(max(0.04, duration_s) * fps))


# --------------------------------------------------------------------------- #
# Rungs 2 & 3 — Ken-Burns over a still
# --------------------------------------------------------------------------- #


def _ken_burns_filter(size: tuple[int, int], frames: int, fps: int, zoom_max: float) -> str:
    """Build the zoompan filtergraph for a smooth, jitter-free slow zoom.

    A single still is fed (no ``-loop``), so ``zoompan d=frames`` generates the
    whole sequence with a *continuously* accumulating zoom (the canonical
    Ken-Burns recipe — looping the input resets the zoom every N frames). The
    still is first up-scaled to 2× the output and centre-cropped to the target
    aspect so the zoom window is sub-pixel smooth at the output resolution.
    """
    out_w, out_h = size
    work_w, work_h = out_w * 2, out_h * 2
    z_inc = (zoom_max - 1.0) / max(frames - 1, 1)
    return (
        f"scale={work_w}:{work_h}:force_original_aspect_ratio=increase,"
        f"crop={work_w}:{work_h},"
        f"zoompan=z='min(zoom+{z_inc:.6f},{zoom_max:.4f})'"
        f":x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
        f":d={frames}:s={out_w}x{out_h}:fps={fps},"
        f"setsar=1,format=yuv420p"
    )


def ken_burns_over_image(
    image_bytes: bytes,
    duration_s: float,
    *,
    audio_bytes: bytes | None = None,
    size: tuple[int, int] = DEFAULT_SIZE,
    fps: int = DEFAULT_FPS,
    zoom_max: float = DEFAULT_ZOOM_MAX,
) -> bytes:
    """Render a real Ken-Burns mp4 over ``image_bytes`` (the §4.4/§12.4 rung).

    A slow zoom/pan over the still at ``size``/``fps`` for ``duration_s`` seconds,
    muxing ``audio_bytes`` (the narration WAV) when provided. Returns mp4 bytes;
    verify with :func:`probe` / :func:`verify_playable`.

    Raises:
        FfmpegError: when no ffmpeg binary is available or the render fails.
        ValueError: when ``image_bytes`` is empty or ``duration_s`` is non-positive.
    """
    if not image_bytes:
        raise ValueError("image_bytes is empty")
    if duration_s <= 0:
        raise ValueError("duration_s must be positive")

    ffmpeg = get_ffmpeg_exe()
    frames = _frames_for(duration_s, fps)
    vf = _ken_burns_filter(size, frames, fps, zoom_max)

    with tempfile.TemporaryDirectory(prefix="kinora_kb_") as tmp:
        tmp_dir = Path(tmp)
        img_path = tmp_dir / f"still{_sniff_ext(image_bytes)}"
        img_path.write_bytes(image_bytes)
        out_path = tmp_dir / "out.mp4"

        args: list[str] = [ffmpeg, "-y", "-i", str(img_path)]
        audio_path: Path | None = None
        if audio_bytes:
            audio_path = tmp_dir / "narration.wav"
            audio_path.write_bytes(audio_bytes)
            args += ["-i", str(audio_path)]

        args += ["-filter_complex", f"[0:v]{vf}[v]", "-map", "[v]"]
        if audio_path is not None:
            args += ["-map", "1:a:0"]

        args += [
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-pix_fmt",
            "yuv420p",
            "-r",
            str(fps),
            "-t",
            f"{duration_s:.3f}",
        ]
        if audio_path is not None:
            args += ["-c:a", "aac", "-b:a", "128k"]
        else:
            args += ["-an"]
        args += ["-movflags", "+faststart", str(out_path)]

        _run(args)
        clip = out_path.read_bytes()

    logger.info(
        "degrade.ken_burns",
        duration_s=round(duration_s, 3),
        size=f"{size[0]}x{size[1]}",
        fps=fps,
        with_audio=bool(audio_bytes),
        bytes=len(clip),
    )
    return clip


# --------------------------------------------------------------------------- #
# Bottom rung — audio + (client-highlighted) text as a minimal narrated card
# --------------------------------------------------------------------------- #


def audio_text_card(
    duration_s: float,
    *,
    audio_bytes: bytes | None = None,
    size: tuple[int, int] = (1280, 720),
    fps: int = DEFAULT_FPS,
    bg_color: str = "black",
) -> bytes:
    """Render the bottom rung: a solid card the narration plays over (§12.4).

    The highlighted text is painted by the client over the PDF page, not burned
    in — so this is intentionally a minimal solid background carrying the audio.
    Still a real, playable mp4 (the film keeps moving with zero generation cost).
    """
    if duration_s <= 0:
        raise ValueError("duration_s must be positive")
    ffmpeg = get_ffmpeg_exe()
    out_w, out_h = size
    with tempfile.TemporaryDirectory(prefix="kinora_card_") as tmp:
        tmp_dir = Path(tmp)
        out_path = tmp_dir / "out.mp4"
        args: list[str] = [
            ffmpeg,
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"color=c={bg_color}:s={out_w}x{out_h}:r={fps}:d={duration_s:.3f}",
        ]
        audio_path: Path | None = None
        if audio_bytes:
            audio_path = tmp_dir / "narration.wav"
            audio_path.write_bytes(audio_bytes)
            args += ["-i", str(audio_path)]
        args += [
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-pix_fmt",
            "yuv420p",
            "-t",
            f"{duration_s:.3f}",
        ]
        if audio_path is not None:
            args += ["-c:a", "aac", "-b:a", "128k", "-map", "0:v:0", "-map", "1:a:0"]
        else:
            args += ["-an"]
        args += ["-movflags", "+faststart", str(out_path)]
        _run(args)
        clip = out_path.read_bytes()
    logger.info("degrade.audio_card", duration_s=round(duration_s, 3), with_audio=bool(audio_bytes))
    return clip


# --------------------------------------------------------------------------- #
# Frame extraction (the live-path Critic samples frames from a clip)
# --------------------------------------------------------------------------- #


def extract_frames(clip_bytes: bytes, count: int = 4) -> list[bytes]:
    """Sample ``count`` PNG frames evenly across ``clip_bytes`` (for QA).

    Used on the live path to feed the Critic's VL/embedding checks; returns raw
    PNG bytes. Best-effort: a frame that cannot be extracted is skipped.
    """
    if not clip_bytes or count <= 0:
        return []
    ffmpeg = get_ffmpeg_exe()
    try:
        duration = probe(clip_bytes).duration_s
    except FfmpegError:
        duration = 0.0
    frames: list[bytes] = []
    with tempfile.TemporaryDirectory(prefix="kinora_frames_") as tmp:
        tmp_dir = Path(tmp)
        clip_path = tmp_dir / "clip.mp4"
        clip_path.write_bytes(clip_bytes)
        for i in range(count):
            # Sample at the midpoint of each of ``count`` equal slices.
            ts = (duration * (i + 0.5) / count) if duration > 0 else 0.0
            frame_path = tmp_dir / f"frame_{i}.png"
            try:
                _run(
                    [
                        ffmpeg,
                        "-y",
                        "-ss",
                        f"{ts:.3f}",
                        "-i",
                        str(clip_path),
                        "-frames:v",
                        "1",
                        "-f",
                        "image2",
                        str(frame_path),
                    ]
                )
            except FfmpegError:
                continue
            if frame_path.exists():
                frames.append(frame_path.read_bytes())
    return frames


__all__ = [
    "DEFAULT_FPS",
    "DEFAULT_SIZE",
    "DEFAULT_ZOOM_MAX",
    "DegradeRung",
    "FfmpegError",
    "ProbeInfo",
    "audio_text_card",
    "extract_frames",
    "ffmpeg_available",
    "get_ffmpeg_exe",
    "get_ffprobe_exe",
    "inspect",
    "ken_burns_over_image",
    "probe",
    "run_ffmpeg",
    "verify_playable",
]
