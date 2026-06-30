"""``Normalizer`` — transcode any provider clip to Kinora's canonical target.

The thin executor over the pure :mod:`app.video.normalize.plan` layer: probe the
input (if not already probed), build the deterministic ffmpeg arg list for the
configured :class:`~app.video.normalize.targets.NormalizationTarget`, run it, and
return the canonical, stitch-ready clip bytes plus a typed
:class:`~app.video.normalize.NormalizeResult` describing what was produced.

Because every decision lives in the (subprocess-free) plan layer, this class only
owns I/O: writing the input to a temp dir, invoking ffmpeg, reading the output.
The async wrappers offload the blocking subprocess to a worker thread (matching
:mod:`app.render.stitch`'s ``anyio.to_thread`` pattern) so the render worker's
event loop is never blocked.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import anyio
from pydantic import BaseModel, ConfigDict

from app.core.logging import get_logger

from .media_info import MediaInfo
from .plan import build_normalize_args
from .probe import ClipProbe
from .runtime import NormalizeError, get_ffmpeg_exe, run
from .targets import NormalizationTarget

logger = get_logger("app.video.normalize.normalizer")


class NormalizeResult(BaseModel):
    """The outcome of normalising one clip."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    clip_bytes: bytes
    width: int
    height: int
    #: ``True`` when the input already matched the target and was passed through
    #: verbatim (no transcode performed).
    passthrough: bool = False
    #: ``True`` when a silent audio track was synthesised for a video-only source.
    synthesized_audio: bool = False
    #: The probed source info (for telemetry / downstream decisions).
    source: MediaInfo | None = None


class Normalizer:
    """Transcode provider clips to one canonical, interchangeable target.

    Args:
        target: the canonical :class:`NormalizationTarget`. Build it from
            :meth:`NormalizationTarget.from_settings` to honour the ``normalize_*``
            config block, or construct one explicitly for a one-off target.
        probe: an optional :class:`ClipProbe`; one is created if omitted.
        allow_passthrough: when ``True`` (default), a clip already matching the
            target on geometry/fps/codec/pixfmt/range is returned unchanged — a
            free win for a backend that natively emits the canonical shape.
        timeout_s: per-invocation ffmpeg wall-clock ceiling.
    """

    def __init__(
        self,
        target: NormalizationTarget,
        *,
        probe: ClipProbe | None = None,
        allow_passthrough: bool = True,
        timeout_s: float = 240.0,
    ) -> None:
        self._target = target
        self._probe = probe or ClipProbe(timeout_s=min(timeout_s, 60.0))
        self._allow_passthrough = allow_passthrough
        self._timeout = timeout_s

    @property
    def target(self) -> NormalizationTarget:
        return self._target

    def _is_passthrough(self, info: MediaInfo) -> bool:
        if not self._allow_passthrough:
            return False
        t = self._target
        return info.matches_target(
            width=t.width,
            height=t.height,
            fps=t.fps,
            video_codec=t.video_codec,
            pixel_format=t.pixel_format,
            color_range=t.color.range,
        ) and info.has_audio

    def normalize_bytes(self, data: bytes, *, info: MediaInfo | None = None) -> NormalizeResult:
        """Normalise in-memory clip bytes to the canonical target (blocking).

        Args:
            data: the source clip bytes (any provider container/codec).
            info: a pre-computed probe of ``data`` (skips a re-probe when the
                caller already has it).

        Raises:
            ValueError: when ``data`` is empty.
            NormalizeError: when no ffmpeg binary is available or the encode fails.
        """
        if not data:
            raise ValueError("normalize_bytes requires non-empty clip data")
        ffmpeg = get_ffmpeg_exe()
        with tempfile.TemporaryDirectory(prefix="kinora_normalize_") as tmp:
            tmp_dir = Path(tmp)
            in_path = tmp_dir / "in"
            in_path.write_bytes(data)
            probed = info or self._probe.probe_path(str(in_path))

            if self._is_passthrough(probed):
                w, h = probed.dimensions or self._target.dimensions
                logger.info("normalize.passthrough", width=w, height=h, bytes=len(data))
                return NormalizeResult(
                    clip_bytes=data,
                    width=w,
                    height=h,
                    passthrough=True,
                    synthesized_audio=False,
                    source=probed,
                )

            out_path = tmp_dir / "out.mp4"
            plan = build_normalize_args(
                ffmpeg=ffmpeg,
                in_path=str(in_path),
                out_path=str(out_path),
                info=probed,
                target=self._target,
            )
            run(plan.args, timeout=self._timeout)
            out_bytes = out_path.read_bytes()

        logger.info(
            "normalize.transcode",
            in_bytes=len(data),
            out_bytes=len(out_bytes),
            size=f"{plan.out_width}x{plan.out_height}",
            fps=self._target.fps,
            synthesized_audio=plan.synthesized_audio,
        )
        return NormalizeResult(
            clip_bytes=out_bytes,
            width=plan.out_width,
            height=plan.out_height,
            passthrough=False,
            synthesized_audio=plan.synthesized_audio,
            source=probed,
        )

    async def normalize_bytes_async(
        self, data: bytes, *, info: MediaInfo | None = None
    ) -> NormalizeResult:
        """Async wrapper running the blocking transcode on a worker thread."""
        return await anyio.to_thread.run_sync(lambda: self.normalize_bytes(data, info=info))

    @classmethod
    def from_settings(cls, settings: object, **kwargs: object) -> Normalizer:
        """Build a :class:`Normalizer` from the ``normalize_*`` settings block."""
        target = NormalizationTarget.from_settings(settings)
        timeout = float(getattr(settings, "normalize_ffmpeg_timeout_s", 240.0))
        return cls(target, timeout_s=timeout, **kwargs)  # type: ignore[arg-type]


def is_normalize_error(exc: BaseException) -> bool:
    """Whether ``exc`` is a normalize-subsystem ffmpeg failure."""
    return isinstance(exc, NormalizeError)


__all__ = ["NormalizeResult", "Normalizer", "is_normalize_error"]
