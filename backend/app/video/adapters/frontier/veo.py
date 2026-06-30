"""Google Veo (Gemini API) adapter.

Veo's async pattern on the Gemini API: POST ``/models/{model}:predictLongRunning``
with ``{instances: [{prompt, image?}], parameters: {aspectRatio, durationSeconds,
sampleCount, negativePrompt, seed, personGeneration}}`` → returns an operation
``{name}`` → POST/GET ``/{operation_name}`` (or ``:fetchPredictOperation``) until
``done`` is true (the result's ``generatedSamples[0].video.uri`` carries the clip).

Quirks handled here:
* auth is the ``x-goog-api-key`` header (not an ``Authorization: Bearer``), so the
  transport is built with ``auth_scheme=""`` plus an explicit header;
* the model id is part of the *path*, so the submit "path" is templated per model;
* the poll endpoint is the *operation name* returned by submit (an absolute-ish path);
* duration is ``durationSeconds`` (int), aspect ratio is ``aspectRatio``; Veo 3 fixes
  output to 720p/1080p and 16:9 or 9:16.
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


def veo_error_mapper(status: int, body: Any) -> FrontierError:
    """Map a Google API error body ({error:{code,message,status}}) → the taxonomy."""
    message = f"HTTP {status}"
    native: str | None = None
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            message = str(err.get("message") or message)
            native = err.get("status")
        else:
            message = str(body.get("message") or message)
    elif isinstance(body, str) and body:
        message = body[:200]
    return build_error(
        code_for_status(status),
        message,
        provider="veo",
        native_code=str(native) if native else None,
        status_code=status,
    )


def _data_uri_to_inline(image: str) -> dict[str, Any]:
    """Convert a data URI to Veo's ``{bytesBase64Encoded, mimeType}`` inline image."""
    if image.startswith("data:") and "," in image:
        header, b64 = image[len("data:") :].split(",", 1)
        mime = header.split(";", 1)[0].strip() or "image/png"
        return {"bytesBase64Encoded": b64, "mimeType": mime}
    # An http(s) URL: Veo accepts a GCS/HTTP uri form too.
    return {"gcsUri": image} if image.startswith("gs://") else {"uri": image}


class VeoAdapter(BaseFrontierAdapter):
    """Google Veo 3 text/image-to-video adapter (Gemini API)."""

    provider_slug = "veo"

    def _default_model(self, settings: Settings) -> str:
        return settings.veo_model

    def _capabilities(self) -> CapabilityProfile:
        return CapabilityProfile(
            provider="veo",
            model=self._model,
            modes=frozenset({VideoMode.TEXT_TO_VIDEO, VideoMode.IMAGE_TO_VIDEO}),
            durations_s=(4.0, 6.0, 8.0),
            resolutions=("720p", "1080p"),
            aspect_ratios=("16:9", "9:16"),
            fps_options=(24,),
            supports_seed=True,
            supports_negative_prompt=True,
            max_reference_images=1,
            max_prompt_chars=1024,
        )

    def _build_submit(self, request: FrontierRequest) -> tuple[str, dict[str, Any]]:
        instance: dict[str, Any] = {"prompt": request.prompt}
        if request.mode is VideoMode.IMAGE_TO_VIDEO:
            image = request.primary_image()
            if not image:
                raise build_error(
                    code_for_status(400),
                    "veo image_to_video has no image input",
                    provider="veo",
                    status_code=400,
                )
            instance["image"] = _data_uri_to_inline(image)
        parameters: dict[str, Any] = {
            "aspectRatio": request.aspect_ratio,
            "durationSeconds": int(request.duration_s),
            "sampleCount": 1,
            "resolution": request.resolution,
        }
        if request.negative_prompt:
            parameters["negativePrompt"] = request.negative_prompt
        if request.seed is not None:
            parameters["seed"] = request.seed
        body = {"instances": [instance], "parameters": parameters}
        return f"models/{self._model}:predictLongRunning", body

    def _parse_submit(self, body: dict[str, Any]) -> str:
        # The long-running operation name, e.g. "models/veo-3.0/operations/abc123".
        return str(body.get("name") or "")

    def _build_poll(self, handle: SubmitHandle) -> tuple[str, str, dict[str, Any] | None]:
        # The operation name *is* the poll path (GET on the Gemini API).
        return "GET", handle.job_id, None

    def _parse_poll(self, body: dict[str, Any]) -> PollResult:
        if not body.get("done"):
            return PollResult(status=JobStatus.PENDING)
        if "error" in body and body["error"]:
            err = body["error"]
            detail = err.get("message") if isinstance(err, dict) else str(err)
            return PollResult(status=JobStatus.FAILED, detail=detail)
        url = self._extract_uri(body.get("response") or {})
        if not url:
            raise FrontierBadResponse(
                "veo operation done but no generated video uri", provider="veo"
            )
        return PollResult(status=JobStatus.SUCCEEDED, asset_url=url, progress=1.0)

    @staticmethod
    def _extract_uri(response: Any) -> str | None:
        if not isinstance(response, dict):
            return None
        # Veo nests the sample under generateVideoResponse.generatedSamples[].video.uri
        gvr = response.get("generateVideoResponse") or response
        samples = gvr.get("generatedSamples") if isinstance(gvr, dict) else None
        if isinstance(samples, list) and samples:
            video = samples[0].get("video") if isinstance(samples[0], dict) else None
            uri = video.get("uri") if isinstance(video, dict) else None
            if isinstance(uri, str) and uri:
                return uri
        # Fallback: a flat predictions[].videoUri shape.
        preds = response.get("predictions")
        if isinstance(preds, list) and preds:
            uri = preds[0].get("videoUri") if isinstance(preds[0], dict) else None
            if isinstance(uri, str) and uri:
                return uri
        return None


def build_veo_adapter(
    settings: Settings,
    *,
    transport: Any | None = None,
    **kwargs: Any,
) -> VeoAdapter:
    tx = transport or FrontierTransport(
        base_url=settings.veo_base_url,
        api_key=settings.veo_api_key,
        provider="veo",
        enabled=settings.frontier_video_enabled,
        error_mapper=veo_error_mapper,
        auth_scheme="",  # Google uses x-goog-api-key, not Authorization: Bearer.
        extra_headers=({"x-goog-api-key": settings.veo_api_key} if settings.veo_api_key else {}),
    )
    return VeoAdapter(settings, tx, **kwargs)


__all__ = ["VeoAdapter", "build_veo_adapter", "veo_error_mapper"]
