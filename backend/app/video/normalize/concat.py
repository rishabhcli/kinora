"""Clip concatenation with a stream-copy fast path + a safe re-encode fallback.

Two clips only concatenate cleanly when they share geometry / fps / codec /
pixel-format / audio layout. This helper:

* probes every input,
* takes the cheap, lossless **demuxer concat** (stream copy, no re-encode) when
  :func:`app.video.normalize.plan.streams_are_uniform` confirms they already
  match, and
* otherwise **normalises each input to a common target first**, then runs the
  robust filter-graph concat (re-encode) — so a Wan clip, a MiniMax clip and a
  degraded Ken-Burns rung join into one valid file regardless of their origins.

This is the provider-agnostic complement to :func:`app.render.stitch.concat_clips`
(which always re-encodes to the film geometry): here the uniform fast path avoids
a needless re-encode when the inputs are already canonical.
"""

from __future__ import annotations

import tempfile
from collections.abc import Sequence
from pathlib import Path

import anyio
from pydantic import BaseModel, ConfigDict

from app.core.logging import get_logger

from .media_info import MediaInfo
from .normalizer import Normalizer
from .plan import build_concat_demux_args, build_concat_reencode_args, streams_are_uniform
from .probe import ClipProbe
from .runtime import get_ffmpeg_exe, run
from .targets import NormalizationTarget

logger = get_logger("app.video.normalize.concat")


class ConcatResult(BaseModel):
    """The outcome of concatenating clips."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    clip_bytes: bytes
    clip_count: int
    #: ``True`` when the inputs were uniform and joined by stream copy (no encode).
    stream_copied: bool
    #: ``True`` when one or more inputs were normalised before the join.
    normalized_inputs: bool


def _concat_manifest(paths: Sequence[Path]) -> str:
    """A concat-demuxer manifest body (``file '...'`` lines, quotes escaped)."""
    return "\n".join(f"file '{str(p)}'" for p in paths) + "\n"


def concat_clips(
    clips: Sequence[bytes],
    *,
    target: NormalizationTarget,
    probe: ClipProbe | None = None,
    normalizer: Normalizer | None = None,
    force_reencode: bool = False,
    timeout_s: float = 240.0,
) -> ConcatResult:
    """Concatenate ``clips`` into one mp4, copying when uniform, else re-encoding.

    Args:
        clips: the ordered clip byte-strings (any mix of providers / degrade rungs).
        target: the canonical target each input is normalised to before a
            re-encode join (and the encode params for that join).
        probe: optional shared :class:`ClipProbe`.
        normalizer: optional shared :class:`Normalizer`; one matching ``target`` is
            created when omitted.
        force_reencode: skip the uniform stream-copy fast path and always
            normalise + re-encode (use when downstream needs the exact target even
            if the inputs happened to already agree with each other).
        timeout_s: per-invocation ffmpeg ceiling.

    Raises:
        ValueError: when ``clips`` is empty.
        NormalizeError: when no ffmpeg binary is available or a step fails.
    """
    if not clips:
        raise ValueError("concat_clips requires at least one clip")
    ffmpeg = get_ffmpeg_exe()
    prober = probe or ClipProbe(timeout_s=min(timeout_s, 60.0))

    if len(clips) == 1:
        # A single clip: re-encode to the target only when forced, else pass through.
        if not force_reencode:
            return ConcatResult(
                clip_bytes=bytes(clips[0]),
                clip_count=1,
                stream_copied=True,
                normalized_inputs=False,
            )
        norm = normalizer or Normalizer(target, probe=prober, timeout_s=timeout_s)
        result = norm.normalize_bytes(clips[0])
        return ConcatResult(
            clip_bytes=result.clip_bytes,
            clip_count=1,
            stream_copied=False,
            normalized_inputs=not result.passthrough,
        )

    infos: list[MediaInfo] = [prober.probe_bytes(c) for c in clips]
    uniform = streams_are_uniform(infos)

    with tempfile.TemporaryDirectory(prefix="kinora_vconcat_") as tmp:
        tmp_dir = Path(tmp)
        out_path = tmp_dir / "joined.mp4"

        if uniform and not force_reencode:
            seg_paths: list[Path] = []
            for i, clip in enumerate(clips):
                seg = tmp_dir / f"seg_{i}.mp4"
                seg.write_bytes(clip)
                seg_paths.append(seg)
            list_path = tmp_dir / "concat.txt"
            list_path.write_text(_concat_manifest(seg_paths), encoding="utf-8")
            run(
                build_concat_demux_args(
                    ffmpeg=ffmpeg, list_path=str(list_path), out_path=str(out_path)
                ),
                timeout=timeout_s,
            )
            joined = out_path.read_bytes()
            logger.info("normalize.concat.copy", clips=len(clips), bytes=len(joined))
            return ConcatResult(
                clip_bytes=joined,
                clip_count=len(clips),
                stream_copied=True,
                normalized_inputs=False,
            )

        # Non-uniform (or forced): normalise every input to the target, then the
        # robust filter-graph concat re-encodes the now-uniform clips into one.
        norm = normalizer or Normalizer(target, probe=prober, timeout_s=timeout_s)
        norm_paths: list[str] = []
        for i, (clip, info) in enumerate(zip(clips, infos, strict=True)):
            result = norm.normalize_bytes(clip, info=info)
            seg = tmp_dir / f"norm_{i}.mp4"
            seg.write_bytes(result.clip_bytes)
            norm_paths.append(str(seg))
        plan = build_concat_reencode_args(
            ffmpeg=ffmpeg, in_paths=norm_paths, out_path=str(out_path), target=target
        )
        run(plan.args, timeout=timeout_s)
        joined = out_path.read_bytes()

    logger.info(
        "normalize.concat.reencode",
        clips=len(clips),
        bytes=len(joined),
        size=f"{target.width}x{target.height}",
    )
    return ConcatResult(
        clip_bytes=joined,
        clip_count=len(clips),
        stream_copied=False,
        normalized_inputs=True,
    )


async def concat_clips_async(
    clips: Sequence[bytes],
    *,
    target: NormalizationTarget,
    probe: ClipProbe | None = None,
    normalizer: Normalizer | None = None,
    force_reencode: bool = False,
    timeout_s: float = 240.0,
) -> ConcatResult:
    """Async wrapper running the blocking concat on a worker thread."""
    return await anyio.to_thread.run_sync(
        lambda: concat_clips(
            clips,
            target=target,
            probe=probe,
            normalizer=normalizer,
            force_reencode=force_reencode,
            timeout_s=timeout_s,
        )
    )


__all__ = ["ConcatResult", "concat_clips", "concat_clips_async"]
