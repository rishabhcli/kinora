"""Runway Gen-3 / Gen-4 adapter.

Runway's async REST API: POST an ``image_to_video`` / ``text_to_video`` task → it
returns a task ``id`` → GET ``/tasks/{id}`` until ``status`` is ``SUCCEEDED`` (the
``output`` array carries the clip URL). Gen-4 Turbo accepts 5s/10s clips at a fixed
menu of aspect-ratio "ratios" (Runway encodes resolution as a ``WxH`` ratio token),
takes a seed + a structured ``promptImage`` for image-to-video, and enforces a
1000-character prompt cap.

Quirks handled here:
* Runway uses ``ratio`` tokens like ``1280:720`` rather than free resolutions; we
  declare the supported ratios as the profile's aspect ratios and map our resolution
  label to the matching ratio token.
* The API version header (``X-Runway-Version``) is required.
* Image-to-video wants a ``promptImage`` (URL or data URI); text-to-video uses
  ``promptText`` only.
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

_RUNWAY_VERSION = "2024-11-06"

#: Map our resolution label → Runway's ratio token (Gen-4 Turbo menu).
_RES_TO_RATIO_16_9 = {"720p": "1280:720", "1080p": "1920:1080"}
_RES_TO_RATIO_9_16 = {"720p": "720:1280", "1080p": "1080:1920"}

_STATUS = {
    "SUCCEEDED": JobStatus.SUCCEEDED,
    "FAILED": JobStatus.FAILED,
    "CANCELLED": JobStatus.CANCELED,
    "CANCELED": JobStatus.CANCELED,
    "PENDING": JobStatus.PENDING,
    "RUNNING": JobStatus.PENDING,
    "THROTTLED": JobStatus.PENDING,
}


def runway_error_mapper(status: int, body: Any) -> FrontierError:
    """Map a Runway error body → the frontier taxonomy."""
    message = f"HTTP {status}"
    native: str | None = None
    if isinstance(body, dict):
        message = str(body.get("error") or body.get("message") or message)
        native = body.get("code")
    elif isinstance(body, str) and body:
        message = body[:200]
    return build_error(
        code_for_status(status),
        message,
        provider="runway",
        native_code=str(native) if native else None,
        status_code=status,
    )


class RunwayAdapter(BaseFrontierAdapter):
    """Runway Gen-4 Turbo (and Gen-3) image/text-to-video adapter."""

    provider_slug = "runway"

    def _default_model(self, settings: Settings) -> str:
        return settings.runway_model

    def _capabilities(self) -> CapabilityProfile:
        return CapabilityProfile(
            provider="runway",
            model=self._model,
            modes=frozenset({VideoMode.TEXT_TO_VIDEO, VideoMode.IMAGE_TO_VIDEO}),
            durations_s=(5.0, 10.0),
            resolutions=("720p", "1080p"),
            aspect_ratios=("16:9", "9:16"),
            fps_options=(24,),
            supports_seed=True,
            supports_negative_prompt=False,
            max_reference_images=1,
            max_prompt_chars=1000,
        )

    def _ratio_token(self, request: FrontierRequest) -> str:
        table = _RES_TO_RATIO_9_16 if request.aspect_ratio == "9:16" else _RES_TO_RATIO_16_9
        return table.get(request.resolution, table["720p"])

    def _build_submit(self, request: FrontierRequest) -> tuple[str, dict[str, Any]]:
        body: dict[str, Any] = {
            "model": self._model,
            "ratio": self._ratio_token(request),
            "duration": int(request.duration_s),
            "promptText": request.prompt,
        }
        if request.seed is not None:
            body["seed"] = request.seed
        if request.mode is VideoMode.IMAGE_TO_VIDEO:
            image = request.primary_image()
            if not image:
                raise build_error(
                    code_for_status(400),
                    "runway image_to_video has no promptImage input",
                    provider="runway",
                    status_code=400,
                )
            body["promptImage"] = image
            return "image_to_video", body
        return "text_to_video", body

    def _parse_submit(self, body: dict[str, Any]) -> str:
        return str(body.get("id") or "")

    def _build_poll(self, handle: SubmitHandle) -> tuple[str, str, dict[str, Any] | None]:
        return "GET", f"tasks/{handle.job_id}", None

    def _parse_poll(self, body: dict[str, Any]) -> PollResult:
        status = _STATUS.get(str(body.get("status") or "").upper(), JobStatus.PENDING)
        if status is JobStatus.SUCCEEDED:
            output = body.get("output")
            url = output[0] if isinstance(output, list) and output else None
            if not isinstance(url, str) or not url:
                raise FrontierBadResponse(
                    "runway task SUCCEEDED but output had no clip url", provider="runway"
                )
            return PollResult(status=status, asset_url=url, progress=1.0)
        return PollResult(
            status=status,
            detail=str(body.get("failure") or body.get("failureCode") or "") or None,
            progress=body.get("progress"),
        )


def build_runway_adapter(
    settings: Settings,
    *,
    transport: Any | None = None,
    **kwargs: Any,
) -> RunwayAdapter:
    """Construct a :class:`RunwayAdapter` with its configured transport."""
    tx = transport or FrontierTransport(
        base_url=settings.runway_base_url,
        api_key=settings.runway_api_key,
        provider="runway",
        enabled=settings.frontier_video_enabled,
        error_mapper=runway_error_mapper,
        extra_headers={"X-Runway-Version": _RUNWAY_VERSION},
    )
    return RunwayAdapter(settings, tx, **kwargs)


__all__ = ["RunwayAdapter", "build_runway_adapter", "runway_error_mapper"]
