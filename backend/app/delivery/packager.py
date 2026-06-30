"""The ABR packager — the thin ffmpeg-executing layer over the pure plan layer.

Everything *decided* here lives in the pure modules (:mod:`ladder`,
:mod:`profiles`, :mod:`segmenter`, :mod:`manifest`); this module only *runs* the
plans with a resolved ffmpeg binary and writes the resulting artifacts. It
reuses the render layer's hardened ffmpeg resolution + runner
(:mod:`app.render.degrade`) so there is exactly one ffmpeg discovery path in the
codebase (``KINORA_FFMPEG`` > system > imageio bundle).

All execution methods are guarded so that *constructing* a packager and building
plans never needs ffmpeg — only :meth:`transcode_rendition` /
:meth:`package_shot` actually shell out, and they raise :class:`PackagingError`
(wrapping the render layer's ``FfmpegError``) if no binary is available. This
keeps the subsystem import-safe and the plan layer fully testable offline.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import anyio
from pydantic import BaseModel, ConfigDict

from app.core.logging import get_logger
from app.delivery.errors import PackagingError
from app.delivery.ladder import Rendition
from app.delivery.profiles import (
    NormalizationSpec,
    ProviderProfile,
    normalization_spec,
    profile_for,
)
from app.delivery.segmenter import (
    EncodePlan,
    SegmentationPlan,
    build_encode_plan,
    build_hls_segmenting_plan,
)

logger = get_logger("app.delivery.packager")


class PackagedRendition(BaseModel):
    """The on-disk result of packaging one shot at one rendition into CMAF."""

    model_config = ConfigDict(extra="forbid")

    rendition_name: str
    init_path: str
    segment_paths: list[str]
    media_playlist_path: str | None
    declared_segment_durations: list[float]


class AbrPackager:
    """Runs the pure packaging plans with a real ffmpeg binary.

    Construct freely (no ffmpeg needed); call the ``*_async``/sync execution
    methods only where an ffmpeg binary is present. Binary resolution + the
    hardened subprocess runner are reused from :mod:`app.render.degrade`.
    """

    def __init__(self, *, ffmpeg_bin: str | None = None) -> None:
        self._ffmpeg_bin = ffmpeg_bin

    def _resolve_ffmpeg(self) -> str:
        if self._ffmpeg_bin:
            return self._ffmpeg_bin
        # Imported lazily so importing this module needs no ffmpeg.
        from app.render.degrade import FfmpegError, get_ffmpeg_exe

        try:
            return get_ffmpeg_exe()
        except FfmpegError as exc:  # pragma: no cover - exercised only without ffmpeg
            raise PackagingError(str(exc)) from exc

    def _run(self, args: list[str]) -> None:
        from app.render.degrade import FfmpegError, run_ffmpeg

        try:
            run_ffmpeg(args)
        except FfmpegError as exc:
            raise PackagingError(f"ffmpeg packaging failed: {exc}") from exc

    # -- plan builders (pure; here for one-call convenience) -----------------

    @staticmethod
    def spec_for(
        provider: str | None, *, fps: int, segment_duration_s: float
    ) -> tuple[ProviderProfile, NormalizationSpec]:
        """Resolve the provider profile + normalization spec for a clip (pure)."""
        profile = profile_for(provider)
        spec = normalization_spec(profile, fps=fps, segment_duration_s=segment_duration_s)
        return profile, spec

    def encode_plan(
        self, *, source: str, rendition: Rendition, spec: NormalizationSpec, output: str
    ) -> EncodePlan:
        return build_encode_plan(source=source, rendition=rendition, spec=spec, output=output)

    def hls_segmenting_plan(
        self,
        *,
        source: str,
        rendition: Rendition,
        spec: NormalizationSpec,
        segment_durations: list[float],
        segment_dir: str,
    ) -> SegmentationPlan:
        return build_hls_segmenting_plan(
            source=source,
            rendition=rendition,
            spec=spec,
            segment_durations=segment_durations,
            segment_dir=segment_dir,
        )

    # -- execution (ffmpeg-gated) -------------------------------------------

    def package_rendition(
        self,
        *,
        source: str,
        rendition: Rendition,
        spec: NormalizationSpec,
        segment_durations: list[float],
        out_dir: str,
    ) -> PackagedRendition:
        """Normalize + segment one shot at one rendition into ``out_dir`` (CMAF/HLS).

        Builds the fused plan and runs it with the resolved ffmpeg. The declared
        segment durations are returned alongside the produced paths so the
        manifest layer asserts the muxer honoured the plan.

        Raises:
            PackagingError: if ffmpeg is unavailable or the run fails.
        """
        ffmpeg_bin = self._resolve_ffmpeg()
        rdir = Path(out_dir) / rendition.name
        rdir.mkdir(parents=True, exist_ok=True)
        plan = self.hls_segmenting_plan(
            source=source,
            rendition=rendition,
            spec=spec,
            segment_durations=segment_durations,
            segment_dir=str(rdir),
        )
        self._run(plan.with_binary(ffmpeg_bin))
        produced = sorted(str(p) for p in rdir.glob("seg_*.m4s"))
        return PackagedRendition(
            rendition_name=rendition.name,
            init_path=plan.init_output,
            segment_paths=produced,
            media_playlist_path=plan.media_playlist,
            declared_segment_durations=plan.segment_durations,
        )

    async def package_rendition_async(
        self,
        *,
        source: str,
        rendition: Rendition,
        spec: NormalizationSpec,
        segment_durations: list[float],
        out_dir: str,
    ) -> PackagedRendition:
        """Async wrapper running the (blocking) ffmpeg packaging in a worker thread."""
        return await anyio.to_thread.run_sync(
            lambda: self.package_rendition(
                source=source,
                rendition=rendition,
                spec=spec,
                segment_durations=segment_durations,
                out_dir=out_dir,
            )
        )

    def make_temp_dir(self, *, prefix: str = "kinora_abr_") -> str:
        """A scratch dir for packaging output (caller owns cleanup)."""
        return tempfile.mkdtemp(prefix=prefix)
