"""Kling (Kuaishou) v2 adapter.

Kling's async REST API: POST ``/videos/image2video`` or ``/videos/text2video`` with
``{model_name, prompt, negative_prompt, duration, aspect_ratio, cfg_scale, image,
image_tail}`` → returns ``{code, data: {task_id, task_status}}`` → GET
``/videos/.../{task_id}`` until ``data.task_status`` is ``succeed`` (the
``task_result.videos[0].url`` carries the clip). Kling supports first-last-frame via
``image`` (start) + ``image_tail`` (end).

Quirks handled here:
* responses are wrapped in ``{code, message, data}`` — a non-zero ``code`` in a 200
  body is still an error, mapped via the taxonomy;
* duration is a *string* token (``"5"`` / ``"10"``);
* Kling images are base64 *without* the ``data:`` prefix — the adapter strips a
  ``data:`` header if present so callers can pass either form;
* auth is a JWT bearer (the caller supplies the already-signed token as the API key).
"""

from __future__ import annotations

from typing import Any

from app.core.config import Settings

from .base import BaseFrontierAdapter
from .errors import (
    FrontierBadResponse,
    FrontierError,
    FrontierErrorCode,
    build_error,
    code_for_status,
)
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
    "succeed": JobStatus.SUCCEEDED,
    "failed": JobStatus.FAILED,
    "submitted": JobStatus.PENDING,
    "processing": JobStatus.PENDING,
}

#: Kling business codes that mean "throttled / capacity" vs "bad request".
_THROTTLE_CODES = {1102, 1103, 5000}


def _strip_data_uri(image: str) -> str:
    """Kling wants raw base64 (no ``data:image/...;base64,`` header) or an http url."""
    if image.startswith(("http://", "https://")):
        return image
    if image.startswith("data:") and "," in image:
        return image.split(",", 1)[1]
    return image


def kling_error_mapper(status: int, body: Any) -> FrontierError:
    message = f"HTTP {status}"
    native: str | None = None
    if isinstance(body, dict):
        message = str(body.get("message") or message)
        native = str(body.get("code")) if body.get("code") is not None else None
    elif isinstance(body, str) and body:
        message = body[:200]
    return build_error(
        code_for_status(status),
        message,
        provider="kling",
        native_code=native,
        status_code=status,
    )


class KlingAdapter(BaseFrontierAdapter):
    """Kling v2 text/image-to-video + first-last-frame adapter."""

    provider_slug = "kling"

    def _default_model(self, settings: Settings) -> str:
        return settings.kling_model

    def _capabilities(self) -> CapabilityProfile:
        return CapabilityProfile(
            provider="kling",
            model=self._model,
            modes=frozenset(
                {
                    VideoMode.TEXT_TO_VIDEO,
                    VideoMode.IMAGE_TO_VIDEO,
                    VideoMode.FIRST_LAST_FRAME,
                }
            ),
            durations_s=(5.0, 10.0),
            resolutions=("720p", "1080p"),
            aspect_ratios=("16:9", "9:16", "1:1"),
            fps_options=(),
            supports_seed=False,
            supports_negative_prompt=True,
            max_reference_images=2,
            max_prompt_chars=2500,
        )

    def _build_submit(self, request: FrontierRequest) -> tuple[str, dict[str, Any]]:
        body: dict[str, Any] = {
            "model_name": self._model,
            "prompt": request.prompt,
            "duration": str(int(request.duration_s)),
            "aspect_ratio": request.aspect_ratio,
            "cfg_scale": 0.5,
        }
        if request.negative_prompt:
            body["negative_prompt"] = request.negative_prompt
        if request.mode is VideoMode.TEXT_TO_VIDEO:
            return "videos/text2video", body
        # image2video covers both i2v and first-last-frame.
        if request.mode is VideoMode.FIRST_LAST_FRAME:
            if request.first_frame_url:
                body["image"] = _strip_data_uri(request.first_frame_url)
            if request.last_frame_url:
                body["image_tail"] = _strip_data_uri(request.last_frame_url)
            if "image" not in body and "image_tail" not in body:
                raise build_error(
                    code_for_status(400),
                    "kling first_last_frame has neither image nor image_tail",
                    provider="kling",
                    status_code=400,
                )
        else:
            image = request.primary_image()
            if not image:
                raise build_error(
                    code_for_status(400),
                    "kling image2video has no image input",
                    provider="kling",
                    status_code=400,
                )
            body["image"] = _strip_data_uri(image)
        return "videos/image2video", body

    @staticmethod
    def _check_business_code(body: dict[str, Any]) -> None:
        """A 200 body with a non-zero ``code`` is still an error (Kling convention)."""
        code = body.get("code")
        if code in (0, None):
            return
        message = str(body.get("message") or f"kling business code {code}")
        canonical = (
            FrontierErrorCode.RATE_LIMITED
            if code in _THROTTLE_CODES
            else FrontierErrorCode.INVALID_REQUEST
        )
        raise build_error(
            canonical, message, provider="kling", native_code=str(code), status_code=200
        )

    def _parse_submit(self, body: dict[str, Any]) -> str:
        self._check_business_code(body)
        data = body.get("data") or {}
        return str(data.get("task_id") or "") if isinstance(data, dict) else ""

    def _build_poll(self, handle: SubmitHandle) -> tuple[str, str, dict[str, Any] | None]:
        # Kling polls the same family path with the task id appended.
        return "GET", f"videos/image2video/{handle.job_id}", None

    def _parse_poll(self, body: dict[str, Any]) -> PollResult:
        self._check_business_code(body)
        data = body.get("data") or {}
        if not isinstance(data, dict):
            raise FrontierBadResponse("kling poll body had no data object", provider="kling")
        status = _STATUS.get(str(data.get("task_status") or "").lower(), JobStatus.PENDING)
        if status is JobStatus.SUCCEEDED:
            result = data.get("task_result") or {}
            videos = result.get("videos") if isinstance(result, dict) else None
            url = videos[0].get("url") if isinstance(videos, list) and videos else None
            if not isinstance(url, str) or not url:
                raise FrontierBadResponse(
                    "kling task succeed but task_result had no video url", provider="kling"
                )
            return PollResult(status=status, asset_url=url, progress=1.0)
        return PollResult(status=status, detail=str(data.get("task_status_msg") or "") or None)


def build_kling_adapter(
    settings: Settings,
    *,
    transport: Any | None = None,
    **kwargs: Any,
) -> KlingAdapter:
    tx = transport or FrontierTransport(
        base_url=settings.kling_base_url,
        api_key=settings.kling_api_key,
        provider="kling",
        enabled=settings.frontier_video_enabled,
        error_mapper=kling_error_mapper,
    )
    return KlingAdapter(settings, tx, **kwargs)


__all__ = ["KlingAdapter", "build_kling_adapter", "kling_error_mapper"]
