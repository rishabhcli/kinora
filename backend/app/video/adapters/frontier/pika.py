"""Pika (2.2) adapter.

Pika's async REST API: POST ``/generate`` with ``{promptText, model, options:
{aspectRatio, resolution, duration, seed, negativePrompt}}`` and an optional
``image`` (image-to-video) → returns ``{id}`` → GET ``/videos/{id}`` until
``status`` is ``finished`` (``url`` carries the clip). Pika 2.2 tops out at 1080p,
5s/10s, 16:9 or 9:16, and exposes both a seed and a negative prompt.

Quirks handled here:
* the request nests creative params under ``options``;
* image-to-video passes a single ``image`` URL/data URI;
* Pika has a documented prompt cap (~512 chars) we declare so over-long prompts are
  rejected locally before a network call.
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
    "finished": JobStatus.SUCCEEDED,
    "succeeded": JobStatus.SUCCEEDED,
    "failed": JobStatus.FAILED,
    "error": JobStatus.FAILED,
    "cancelled": JobStatus.CANCELED,
    "canceled": JobStatus.CANCELED,
    "pending": JobStatus.PENDING,
    "queued": JobStatus.PENDING,
    "processing": JobStatus.PENDING,
    "generating": JobStatus.PENDING,
}


def pika_error_mapper(status: int, body: Any) -> FrontierError:
    message = f"HTTP {status}"
    native: str | None = None
    if isinstance(body, dict):
        message = str(body.get("message") or body.get("error") or message)
        native = body.get("code")
    elif isinstance(body, str) and body:
        message = body[:200]
    return build_error(
        code_for_status(status),
        message,
        provider="pika",
        native_code=str(native) if native else None,
        status_code=status,
    )


class PikaAdapter(BaseFrontierAdapter):
    """Pika 2.2 text/image-to-video adapter."""

    provider_slug = "pika"

    def _default_model(self, settings: Settings) -> str:
        return settings.pika_model

    def _capabilities(self) -> CapabilityProfile:
        return CapabilityProfile(
            provider="pika",
            model=self._model,
            modes=frozenset({VideoMode.TEXT_TO_VIDEO, VideoMode.IMAGE_TO_VIDEO}),
            durations_s=(5.0, 10.0),
            resolutions=("720p", "1080p"),
            aspect_ratios=("16:9", "9:16", "1:1"),
            fps_options=(24,),
            supports_seed=True,
            supports_negative_prompt=True,
            max_reference_images=1,
            max_prompt_chars=512,
        )

    def _build_submit(self, request: FrontierRequest) -> tuple[str, dict[str, Any]]:
        options: dict[str, Any] = {
            "aspectRatio": request.aspect_ratio,
            "resolution": request.resolution,
            "duration": int(request.duration_s),
        }
        if request.seed is not None:
            options["seed"] = request.seed
        if request.negative_prompt:
            options["negativePrompt"] = request.negative_prompt
        body: dict[str, Any] = {
            "model": self._model,
            "promptText": request.prompt,
            "options": options,
        }
        if request.mode is VideoMode.IMAGE_TO_VIDEO:
            image = request.primary_image()
            if not image:
                raise build_error(
                    code_for_status(400),
                    "pika image_to_video has no image input",
                    provider="pika",
                    status_code=400,
                )
            body["image"] = image
        return "generate", body

    def _parse_submit(self, body: dict[str, Any]) -> str:
        return str(body.get("id") or body.get("video_id") or "")

    def _build_poll(self, handle: SubmitHandle) -> tuple[str, str, dict[str, Any] | None]:
        return "GET", f"videos/{handle.job_id}", None

    def _parse_poll(self, body: dict[str, Any]) -> PollResult:
        status = _STATUS.get(str(body.get("status") or "").lower(), JobStatus.PENDING)
        if status is JobStatus.SUCCEEDED:
            url = body.get("url") or body.get("videoUrl")
            if not isinstance(url, str) or not url:
                raise FrontierBadResponse("pika video finished but had no url", provider="pika")
            return PollResult(status=status, asset_url=url, progress=1.0)
        return PollResult(
            status=status,
            detail=str(body.get("error") or body.get("message") or "") or None,
            progress=body.get("progress"),
        )


def build_pika_adapter(
    settings: Settings,
    *,
    transport: Any | None = None,
    **kwargs: Any,
) -> PikaAdapter:
    tx = transport or FrontierTransport(
        base_url=settings.pika_base_url,
        api_key=settings.pika_api_key,
        provider="pika",
        enabled=settings.frontier_video_enabled,
        error_mapper=pika_error_mapper,
    )
    return PikaAdapter(settings, tx, **kwargs)


__all__ = ["PikaAdapter", "build_pika_adapter", "pika_error_mapper"]
