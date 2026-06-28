"""HLS / DASH packaging + a master playlist for the generated films.

The reading-room player wants to *stream* a stitched scene/event film rather
than wait for one big mp4 — and to adapt the bitrate as the network allows. This
module segments a clip into:

* **HLS** — per-variant media playlists (``.m3u8`` + ``.ts`` segments) and a
  **master playlist** that lists the variants by bandwidth/resolution. This is
  the format Safari / iOS / most players take natively, so it is the default the
  reading room targets.
* **DASH** — an MPD manifest + ``.m4s`` segments (one ABR ladder), for players
  that prefer it.

Two layers:

1. **Pure planning** — :class:`VariantSpec`, :func:`abr_ladder`, and
   :func:`master_playlist` author the text manifests deterministically (fully
   unit-testable, no ffmpeg).
2. **ffmpeg packaging** — :func:`package_hls` / :func:`package_dash` invoke the
   portable ffmpeg to actually segment a clip into a directory of files, which
   the service then uploads under one key prefix.

Outputs are returned as in-memory ``{relative_path: bytes}`` maps so the service
can content-address + upload them without touching the filesystem contract.
"""

from __future__ import annotations

import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from app.core.logging import get_logger
from app.media.errors import PackagingError

logger = get_logger("app.media.packaging")

#: Default HLS target segment duration (seconds). 4s is the streaming sweet spot
#: (small enough to switch variants quickly, large enough for good compression).
DEFAULT_SEGMENT_S = 4

#: Master / variant playlist + manifest filenames used throughout.
MASTER_PLAYLIST_NAME = "master.m3u8"
DASH_MANIFEST_NAME = "manifest.mpd"


@dataclass(frozen=True, slots=True)
class VariantSpec:
    """One rung of the adaptive-bitrate ladder."""

    name: str
    height: int
    bitrate_kbps: int
    #: H.264 profile codec string for the HLS master ``CODECS`` attribute.
    codecs: str = "avc1.4d401f,mp4a.40.2"

    @property
    def bandwidth_bps(self) -> int:
        """The HLS ``BANDWIDTH`` (bits/s), with ~10% audio/overhead headroom."""
        return int(self.bitrate_kbps * 1000 * 1.1)

    def width_for(self, src_w: int, src_h: int) -> int:
        """Even-rounded width that preserves the source aspect at this height."""
        if src_h <= 0:
            return self.height
        w = round(src_w * self.height / src_h)
        return w - (w % 2)  # H.264 needs even dimensions

    def resolution_for(self, src_w: int, src_h: int) -> str:
        """``WIDTHxHEIGHT`` for the HLS ``RESOLUTION`` attribute."""
        return f"{self.width_for(src_w, src_h)}x{self.height}"


#: The default vertical-reel ABR ladder (heights chosen for 720×1280 source).
_DEFAULT_LADDER: tuple[VariantSpec, ...] = (
    VariantSpec("1280p", 1280, 2800),
    VariantSpec("854p", 854, 1400),
    VariantSpec("640p", 640, 800),
)


def abr_ladder(src_height: int | None = None) -> list[VariantSpec]:
    """Return the ABR ladder, dropping rungs above the source height.

    Never upscale: a 640-tall source yields only the ≤640 rungs (plus the source
    height itself if it is between rungs). A ``None`` source returns the full
    default ladder.
    """
    if src_height is None:
        return list(_DEFAULT_LADDER)
    rungs = [v for v in _DEFAULT_LADDER if v.height <= src_height]
    if not rungs:
        # source smaller than the smallest rung → a single source-height rung
        return [VariantSpec(f"{src_height}p", src_height, max(400, src_height))]
    if rungs[0].height < src_height:
        # add a top rung at the native height so we serve full quality too
        rungs = [VariantSpec(f"{src_height}p", src_height, rungs[0].bitrate_kbps + 600), *rungs]
    return rungs


def master_playlist(
    variants: Sequence[tuple[VariantSpec, str]],
    *,
    src_w: int = 720,
    src_h: int = 1280,
) -> str:
    """Author an HLS master playlist from ``(variant, playlist_relpath)`` pairs."""
    lines = ["#EXTM3U", "#EXT-X-VERSION:6"]
    for variant, relpath in variants:
        res = variant.resolution_for(src_w, src_h)
        lines.append(
            f'#EXT-X-STREAM-INF:BANDWIDTH={variant.bandwidth_bps},'
            f'RESOLUTION={res},CODECS="{variant.codecs}"'
        )
        lines.append(relpath)
    return "\n".join(lines) + "\n"


def _ffmpeg() -> str:
    from app.render.degrade import FfmpegError, get_ffmpeg_exe

    try:
        return get_ffmpeg_exe()
    except FfmpegError as exc:  # pragma: no cover
        raise PackagingError(str(exc)) from exc


def _run(args: list[str]) -> None:
    from app.render.degrade import FfmpegError, run_ffmpeg

    try:
        run_ffmpeg(args)
    except FfmpegError as exc:
        raise PackagingError(str(exc)) from exc


def _collect(root: Path) -> dict[str, bytes]:
    """Read every file under ``root`` into a ``{relpath: bytes}`` map."""
    out: dict[str, bytes] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            out[path.relative_to(root).as_posix()] = path.read_bytes()
    return out


@dataclass(frozen=True, slots=True)
class PackageResult:
    """The output of packaging: in-memory files + the entry-point name."""

    files: Mapping[str, bytes]
    entrypoint: str
    variants: tuple[str, ...]

    @property
    def segment_count(self) -> int:
        """Number of media segment files produced."""
        return sum(1 for n in self.files if n.endswith((".ts", ".m4s")))


def package_hls(
    clip_bytes: bytes,
    *,
    segment_s: int = DEFAULT_SEGMENT_S,
    variants: Sequence[VariantSpec] | None = None,
    src_w: int = 0,
    src_h: int = 0,
) -> PackageResult:
    """Segment ``clip_bytes`` into a multi-variant HLS package (real ffmpeg).

    Produces one media playlist + segments per variant and a master playlist.
    The ladder defaults to :func:`abr_ladder` for the probed source height. The
    result is an in-memory file map so the service can upload it under one
    prefix; ``entrypoint`` is :data:`MASTER_PLAYLIST_NAME`.
    """
    if not clip_bytes:
        raise PackagingError("clip_bytes is empty")
    if src_w <= 0 or src_h <= 0:
        src_w, src_h = _probe_geometry(clip_bytes)
    ladder = list(variants) if variants is not None else abr_ladder(src_h or None)
    ffmpeg = _ffmpeg()

    with tempfile.TemporaryDirectory(prefix="kinora_hls_") as tmp:
        root = Path(tmp)
        src = root / "src.mp4"
        src.write_bytes(clip_bytes)
        master_entries: list[tuple[VariantSpec, str]] = []
        for variant in ladder:
            vdir = root / variant.name
            vdir.mkdir(parents=True, exist_ok=True)
            width = variant.width_for(src_w or 720, src_h or 1280)
            _run(
                [
                    ffmpeg,
                    "-y",
                    "-i",
                    str(src),
                    "-vf",
                    f"scale={width}:{variant.height}",
                    "-c:v",
                    "libx264",
                    "-preset",
                    "veryfast",
                    "-b:v",
                    f"{variant.bitrate_kbps}k",
                    "-c:a",
                    "aac",
                    "-b:a",
                    "128k",
                    "-pix_fmt",
                    "yuv420p",
                    "-hls_time",
                    str(segment_s),
                    "-hls_playlist_type",
                    "vod",
                    "-hls_segment_filename",
                    str(vdir / "seg_%03d.ts"),
                    str(vdir / "index.m3u8"),
                ]
            )
            master_entries.append((variant, f"{variant.name}/index.m3u8"))
        master = master_playlist(master_entries, src_w=src_w or 720, src_h=src_h or 1280)
        (root / MASTER_PLAYLIST_NAME).write_text(master)
        files = _collect(root)
        files.pop("src.mp4", None)

    logger.info(
        "media.hls",
        variants=[v.name for v in ladder],
        segment_s=segment_s,
        files=len(files),
    )
    return PackageResult(
        files=files,
        entrypoint=MASTER_PLAYLIST_NAME,
        variants=tuple(v.name for v in ladder),
    )


def package_dash(
    clip_bytes: bytes,
    *,
    segment_s: int = DEFAULT_SEGMENT_S,
) -> PackageResult:
    """Segment ``clip_bytes`` into a single-variant DASH package (real ffmpeg).

    Produces an MPD manifest + ``.m4s`` segments. Kept single-rung (the source
    resolution) — the HLS path carries the ABR ladder; DASH is the
    interoperability fallback. ``entrypoint`` is :data:`DASH_MANIFEST_NAME`.
    """
    if not clip_bytes:
        raise PackagingError("clip_bytes is empty")
    ffmpeg = _ffmpeg()
    with tempfile.TemporaryDirectory(prefix="kinora_dash_") as tmp:
        root = Path(tmp)
        src = root / "src.mp4"
        src.write_bytes(clip_bytes)
        _run(
            [
                ffmpeg,
                "-y",
                "-i",
                str(src),
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-c:a",
                "aac",
                "-b:a",
                "128k",
                "-pix_fmt",
                "yuv420p",
                "-seg_duration",
                str(segment_s),
                "-use_template",
                "1",
                "-use_timeline",
                "1",
                "-f",
                "dash",
                str(root / DASH_MANIFEST_NAME),
            ]
        )
        files = _collect(root)
        files.pop("src.mp4", None)
    logger.info("media.dash", segment_s=segment_s, files=len(files))
    return PackageResult(files=files, entrypoint=DASH_MANIFEST_NAME, variants=("default",))


def _probe_geometry(clip_bytes: bytes) -> tuple[int, int]:
    try:
        from app.media.probe import probe_media

        p = probe_media(clip_bytes)
        return (p.width or 720, p.height or 1280)
    except Exception:  # noqa: BLE001
        return (720, 1280)


__all__ = [
    "DASH_MANIFEST_NAME",
    "DEFAULT_SEGMENT_S",
    "MASTER_PLAYLIST_NAME",
    "PackageResult",
    "VariantSpec",
    "abr_ladder",
    "master_playlist",
    "package_dash",
    "package_hls",
]
