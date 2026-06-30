"""OpenAI Sora adapter.

Sora's async REST API: POST ``/videos`` with ``{model, prompt, size, seconds,
input_reference?}`` → returns a video object ``{id, status}`` → GET ``/videos/{id}``
until ``status`` is ``completed`` (``failed`` carries an ``error``) → download the
bytes from ``GET /videos/{id}/content``. Sora encodes resolution as a pixel ``size``
(``"1280x720"``) and duration as a ``seconds`` *string* (``"4"`` / ``"8"`` / ``"12"``).

Quirks handled here:
* the completed object does **not** hand back an expiring CDN URL; the bytes live at a
  dedicated ``/videos/{id}/content`` sub-resource, so :meth:`fetch` is overridden to
  download from there (re-download eagerly, same contract);
* ``size`` is a ``WxH`` token derived from our resolution + aspect ratio;
* image-to-video passes an ``input_reference`` image; Sora has no negative prompt.
"""

from __future__ import annotations

from typing import Any

from app.core.config import Settings

from .base import BaseFrontierAdapter
from .errors import FrontierError, build_error, code_for_status
from .transport import FrontierTransport
from .types import (
    CapabilityProfile,
    FetchedClip,
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
    "in_progress": JobStatus.PENDING,
    "processing": JobStatus.PENDING,
}

#: resolution label + aspect ratio → Sora ``size`` (WxH) token.
_SIZE = {
    ("720p", "16:9"): "1280x720",
    ("720p", "9:16"): "720x1280",
    ("1080p", "16:9"): "1792x1024",
    ("1080p", "9:16"): "1024x1792",
}


def sora_error_mapper(status: int, body: Any) -> FrontierError:
    """Map an OpenAI error body ({error:{message,code,type}}) → the taxonomy."""
    message = f"HTTP {status}"
    native: str | None = None
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            message = str(err.get("message") or message)
            native = err.get("code") or err.get("type")
    elif isinstance(body, str) and body:
        message = body[:200]
    return build_error(
        code_for_status(status),
        message,
        provider="sora",
        native_code=str(native) if native else None,
        status_code=status,
    )


class SoraAdapter(BaseFrontierAdapter):
    """OpenAI Sora text/image-to-video adapter."""

    provider_slug = "sora"

    def _default_model(self, settings: Settings) -> str:
        return settings.sora_model

    def _capabilities(self) -> CapabilityProfile:
        return CapabilityProfile(
            provider="sora",
            model=self._model,
            modes=frozenset({VideoMode.TEXT_TO_VIDEO, VideoMode.IMAGE_TO_VIDEO}),
            durations_s=(4.0, 8.0, 12.0),
            resolutions=("720p", "1080p"),
            aspect_ratios=("16:9", "9:16"),
            fps_options=(),
            supports_seed=False,
            supports_negative_prompt=False,
            max_reference_images=1,
            max_prompt_chars=0,
        )

    def _size_token(self, request: FrontierRequest) -> str:
        return _SIZE.get((request.resolution, request.aspect_ratio), "1280x720")

    def _build_submit(self, request: FrontierRequest) -> tuple[str, dict[str, Any]]:
        body: dict[str, Any] = {
            "model": self._model,
            "prompt": request.prompt,
            "size": self._size_token(request),
            "seconds": str(int(request.duration_s)),
        }
        if request.mode is VideoMode.IMAGE_TO_VIDEO:
            image = request.primary_image()
            if not image:
                raise build_error(
                    code_for_status(400),
                    "sora image_to_video has no input_reference",
                    provider="sora",
                    status_code=400,
                )
            body["input_reference"] = image
        return "videos", body

    def _parse_submit(self, body: dict[str, Any]) -> str:
        return str(body.get("id") or "")

    def _build_poll(self, handle: SubmitHandle) -> tuple[str, str, dict[str, Any] | None]:
        return "GET", f"videos/{handle.job_id}", None

    def _parse_poll(self, body: dict[str, Any]) -> PollResult:
        status = _STATUS.get(str(body.get("status") or "").lower(), JobStatus.PENDING)
        if status is JobStatus.SUCCEEDED:
            # The content lives at /videos/{id}/content — carry the id as the asset
            # "url" path so fetch() downloads from the content sub-resource.
            return PollResult(
                status=status,
                asset_url=f"videos/{body.get('id')}/content",
                progress=1.0,
            )
        detail = None
        err = body.get("error")
        if isinstance(err, dict):
            detail = str(err.get("message") or "")
        return PollResult(status=status, detail=detail or None, progress=body.get("progress"))

    async def fetch(self, poll_result: PollResult) -> FetchedClip:
        """Download Sora bytes from the ``/videos/{id}/content`` sub-resource."""
        if poll_result.status is not JobStatus.SUCCEEDED or not poll_result.asset_url:
            raise FrontierError(
                "sora fetch called without a succeeded content path", provider="sora"
            )
        clip_bytes = await self._transport.download(poll_result.asset_url, op="sora_clip")
        return FetchedClip(
            clip_bytes=clip_bytes,
            last_frame_bytes=None,
            clip_url=poll_result.asset_url,
            duration_s=poll_result.duration_s or 0.0,
        )


def build_sora_adapter(
    settings: Settings,
    *,
    transport: Any | None = None,
    **kwargs: Any,
) -> SoraAdapter:
    tx = transport or FrontierTransport(
        base_url=settings.sora_base_url,
        api_key=settings.sora_api_key,
        provider="sora",
        enabled=settings.frontier_video_enabled,
        error_mapper=sora_error_mapper,
    )
    return SoraAdapter(settings, tx, **kwargs)


__all__ = ["SoraAdapter", "build_sora_adapter", "sora_error_mapper"]
