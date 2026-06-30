"""Luma Dream Machine (Ray-2) adapter.

Luma's async REST API: POST ``/generations`` with ``{prompt, model, resolution,
aspect_ratio, duration, keyframes}`` → returns a generation ``id`` → GET
``/generations/{id}`` until ``state`` is ``completed`` (``assets.video`` holds the
clip URL). Luma is unusual in that *both* start and end frames are supplied as
``keyframes`` (``frame0`` / ``frame1``) — so it natively supports first-last-frame as
well as image-to-video. Duration is a *string* token (``"5s"`` / ``"9s"``).

Quirks handled here:
* keyframes use ``{type: "image", url}`` (a public URL/data URI) under ``frame0`` /
  ``frame1``; image-to-video sets only ``frame0``, first-last sets both.
* resolution is a label (``"720p"`` / ``"1080p"`` / ``"4k"``); aspect_ratio is its own
  field; duration is a ``"<n>s"`` string.
* Luma exposes no seed; a negative prompt is unsupported.
"""

from __future__ import annotations

from typing import Any

from app.core.config import Settings

from .base import BaseFrontierAdapter
from .errors import FrontierBadResponse, FrontierError, build_error, code_for_status
from .transport import FrontierTransport
from .types import (
    CapabilityProfile,
    FrontierRequest,
    JobStatus,
    PollResult,
    SubmitHandle,
    VideoMode,
)

_STATUS = {
    "completed": JobStatus.SUCCEEDED,
    "failed": JobStatus.FAILED,
    "cancelled": JobStatus.CANCELED,
    "canceled": JobStatus.CANCELED,
    "queued": JobStatus.PENDING,
    "dreaming": JobStatus.PENDING,
    "pending": JobStatus.PENDING,
}


def luma_error_mapper(status: int, body: Any) -> FrontierError:
    message = f"HTTP {status}"
    if isinstance(body, dict):
        message = str(body.get("detail") or body.get("message") or message)
    elif isinstance(body, str) and body:
        message = body[:200]
    return build_error(code_for_status(status), message, provider="luma", status_code=status)


class LumaAdapter(BaseFrontierAdapter):
    """Luma Dream Machine (Ray-2) text/image/first-last-frame adapter."""

    provider_slug = "luma"

    def _default_model(self, settings: Settings) -> str:
        return settings.luma_model

    def _capabilities(self) -> CapabilityProfile:
        return CapabilityProfile(
            provider="luma",
            model=self._model,
            modes=frozenset(
                {
                    VideoMode.TEXT_TO_VIDEO,
                    VideoMode.IMAGE_TO_VIDEO,
                    VideoMode.FIRST_LAST_FRAME,
                }
            ),
            durations_s=(5.0, 9.0),
            resolutions=("540p", "720p", "1080p", "4k"),
            aspect_ratios=("16:9", "9:16", "1:1", "4:3", "3:4", "21:9"),
            fps_options=(),
            supports_seed=False,
            supports_negative_prompt=False,
            max_reference_images=2,
            max_prompt_chars=0,
        )

    @staticmethod
    def _keyframe(url: str) -> dict[str, str]:
        return {"type": "image", "url": url}

    def _build_submit(self, request: FrontierRequest) -> tuple[str, dict[str, Any]]:
        body: dict[str, Any] = {
            "model": self._model,
            "prompt": request.prompt,
            "resolution": request.resolution,
            "aspect_ratio": request.aspect_ratio,
            "duration": f"{int(request.duration_s)}s",
        }
        keyframes: dict[str, Any] = {}
        if request.mode is VideoMode.IMAGE_TO_VIDEO:
            image = request.primary_image()
            if not image:
                raise build_error(
                    code_for_status(400),
                    "luma image_to_video has no start keyframe",
                    provider="luma",
                    status_code=400,
                )
            keyframes["frame0"] = self._keyframe(image)
        elif request.mode is VideoMode.FIRST_LAST_FRAME:
            if request.first_frame_url:
                keyframes["frame0"] = self._keyframe(request.first_frame_url)
            if request.last_frame_url:
                keyframes["frame1"] = self._keyframe(request.last_frame_url)
            if not keyframes:
                raise build_error(
                    code_for_status(400),
                    "luma first_last_frame has neither frame0 nor frame1",
                    provider="luma",
                    status_code=400,
                )
        if keyframes:
            body["keyframes"] = keyframes
        return "generations", body

    def _parse_submit(self, body: dict[str, Any]) -> str:
        return str(body.get("id") or "")

    def _build_poll(self, handle: SubmitHandle) -> tuple[str, str, dict[str, Any] | None]:
        return "GET", f"generations/{handle.job_id}", None

    def _parse_poll(self, body: dict[str, Any]) -> PollResult:
        status = _STATUS.get(str(body.get("state") or "").lower(), JobStatus.PENDING)
        if status is JobStatus.SUCCEEDED:
            assets = body.get("assets") or {}
            url = assets.get("video") if isinstance(assets, dict) else None
            if not isinstance(url, str) or not url:
                raise FrontierBadResponse(
                    "luma generation completed but assets.video was empty", provider="luma"
                )
            thumb = assets.get("image") if isinstance(assets, dict) else None
            return PollResult(
                status=status,
                asset_url=url,
                last_frame_url=thumb if isinstance(thumb, str) else None,
                progress=1.0,
            )
        return PollResult(status=status, detail=str(body.get("failure_reason") or "") or None)


def build_luma_adapter(
    settings: Settings,
    *,
    transport: Any | None = None,
    **kwargs: Any,
) -> LumaAdapter:
    tx = transport or FrontierTransport(
        base_url=settings.luma_base_url,
        api_key=settings.luma_api_key,
        provider="luma",
        enabled=settings.frontier_video_enabled,
        error_mapper=luma_error_mapper,
    )
    return LumaAdapter(settings, tx, **kwargs)


__all__ = ["LumaAdapter", "build_luma_adapter", "luma_error_mapper"]
