"""Hosted Wan video synthesis (async submit → poll → fetch) with a hard spend gate.

Implements the §9.3 decision-tree modes, each mapped to the configured hosted
DashScope / Qwen Cloud model id and request shape. The real flow is DashScope's
native async video-synthesis HTTP API: submit a task, poll its status with a
timeout + backoff, then download the clip.

CRITICAL: ``render`` raises :class:`LiveVideoDisabled` unless
``settings.kinora_live_video`` is set. Real Wan renders burn scarce, metered
video-seconds; degradation (Ken-Burns over a keyframe) lives in the render
worker, not here — this provider never fabricates a clip.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from .base import ProviderClient
from .base import sdk_get as _get
from .errors import ModelNotAvailable, ProviderBadRequest, ProviderError
from .types import Usage, VideoResult, WanMode, WanSpec

#: Native async video-synthesis service path.
_VIDEO_PATH = "services/aigc/video-generation/video-synthesis"
_TERMINAL_OK = {"SUCCEEDED"}
_TERMINAL_BAD = {"FAILED", "CANCELED", "UNKNOWN"}
_VIDEO_URL_KEYS = ("video_url", "url", "result_url", "download_url")


class VideoProtocol(StrEnum):
    """Request-shape profile for a hosted Wan model family."""

    #: Current Wan 2.7-style HTTP protocol: ``input.media`` entries typed as
    #: ``first_frame`` / ``last_frame`` / ``first_clip`` / ``reference_image``.
    MEDIA = "media"
    #: Older Wan 2.1/2.2/2.5 style protocol: single-image modes use ``img_url``.
    LEGACY = "legacy"


@dataclass(frozen=True, slots=True)
class VideoModelProfile:
    """Resolved model + request protocol for one Wan render."""

    model: str
    protocol: VideoProtocol


class VideoPollConfig:
    """Polling bounds for an async Wan task."""

    def __init__(
        self,
        *,
        timeout_s: float = 600.0,
        interval_s: float = 3.0,
        max_interval_s: float = 15.0,
    ) -> None:
        self.timeout_s = timeout_s
        self.interval_s = interval_s
        self.max_interval_s = max_interval_s

    @classmethod
    def from_settings(cls, settings: Any) -> VideoPollConfig:
        """Build polling bounds from :class:`app.core.config.Settings`."""
        return cls(
            timeout_s=float(settings.video_poll_timeout_s),
            interval_s=float(settings.video_poll_interval_s),
            max_interval_s=float(settings.video_poll_max_interval_s),
        )


class VideoProvider:
    """Async Wan render client (gated) + cheap model verification.

    Satisfies the :class:`~app.providers.video_router.VideoBackend` protocol — it
    exposes a ``name`` and an ``async healthy()`` probe — so a single hosted Wan
    id is a drop-in member of a :class:`~app.providers.video_router.VideoRouter`
    (failover / racing across several backends) with no caller changes.
    """

    def __init__(
        self,
        client: ProviderClient,
        *,
        poll: VideoPollConfig | None = None,
        name: str | None = None,
    ) -> None:
        self._client = client
        self._settings = client.settings
        self._poll = poll or VideoPollConfig.from_settings(client.settings)
        #: A stable identity for routing/telemetry; defaults to the configured t2v
        #: model id so a single-backend router still has a meaningful name.
        self.name = name or f"video:{client.settings.video_model}"

    async def healthy(self) -> bool:
        """Cheap liveness probe for the router (NO render, honours the gate).

        Returns ``True`` without any network call when the live gate is off — a
        gated-off backend is "available" for routing purposes (the gate, not the
        backend, is what blocks the render); the deliberate ``LiveVideoDisabled``
        is surfaced at :meth:`render`, never here. With the gate on, defers to the
        cheap :meth:`verify_model_available` (empty-input task, no render spend).
        """
        if not self._settings.kinora_live_video:
            return True
        try:
            return await self.verify_model_available()
        except ProviderError:
            return False

    # -- model id resolution per mode ------------------------------------- #

    def _model_for(self, spec: WanSpec) -> str:
        if spec.model:
            return spec.model
        s = self._settings
        return {
            WanMode.TEXT_TO_VIDEO: s.video_model,
            WanMode.IMAGE_TO_VIDEO: s.video_model_i2v,
            WanMode.REFERENCE_TO_VIDEO: s.video_model_r2v,
            # FLF / continuation / instruction-edit ride the image-to-video model
            # (first+last frame, endpoint-frame continuation, source-clip edit).
            WanMode.FIRST_LAST_FRAME: s.video_model_i2v,
            WanMode.VIDEO_CONTINUATION: s.video_model_i2v,
            WanMode.INSTRUCTION_EDIT: s.video_model_i2v,
        }[spec.mode]

    def _profile_for(self, spec: WanSpec) -> VideoModelProfile:
        """Resolve a model profile from the selected id.

        Wan 2.7 uses the newer media-array protocol. The currently reliable demo
        ids in this repo (Wan 2.1/2.2/2.5) use the older single-image fields.
        """
        return self.profile_for_model(self._model_for(spec))

    @staticmethod
    def profile_for_model(model: str) -> VideoModelProfile:
        """Resolve the request protocol for a configured hosted Wan model id."""
        protocol = VideoProtocol.MEDIA if model.startswith("wan2.7-") else VideoProtocol.LEGACY
        return VideoModelProfile(model=model, protocol=protocol)

    def _parameters(self, spec: WanSpec) -> dict[str, Any]:
        params: dict[str, Any] = {
            "duration": spec.duration_s,
            "resolution": spec.resolution,
            "watermark": spec.watermark,
            "prompt_extend": spec.prompt_extend,
        }
        if spec.negative_prompt:
            params["negative_prompt"] = spec.negative_prompt
        if spec.seed is not None:
            params["seed"] = spec.seed
        return params

    def _submit_body(self, spec: WanSpec, profile: VideoModelProfile) -> dict[str, Any]:
        """Translate a :class:`WanSpec` into DashScope native HTTP JSON."""
        input_body: dict[str, Any] = {"prompt": spec.prompt}
        if profile.protocol is VideoProtocol.MEDIA:
            media = self._media_items(spec)
            if media:
                input_body["media"] = media
        else:
            self._fill_legacy_inputs(input_body, spec)
        return {"model": profile.model, "input": input_body, "parameters": self._parameters(spec)}

    def _media_items(self, spec: WanSpec) -> list[dict[str, str]]:
        media: list[dict[str, str]] = []
        if spec.mode is WanMode.TEXT_TO_VIDEO:
            pass
        elif spec.mode in (WanMode.IMAGE_TO_VIDEO, WanMode.VIDEO_CONTINUATION):
            if spec.image_url:
                media.append({"type": "first_frame", "url": spec.image_url})
            if spec.mode is WanMode.VIDEO_CONTINUATION and spec.source_video_url:
                media.append({"type": "first_clip", "url": spec.source_video_url})
        elif spec.mode is WanMode.FIRST_LAST_FRAME:
            if spec.first_frame_url:
                media.append({"type": "first_frame", "url": spec.first_frame_url})
            if spec.last_frame_url:
                media.append({"type": "last_frame", "url": spec.last_frame_url})
        elif spec.mode is WanMode.REFERENCE_TO_VIDEO:
            for url in spec.reference_image_urls:
                item = {"type": "reference_image", "url": url}
                media.append(item)
        elif spec.mode is WanMode.INSTRUCTION_EDIT and spec.source_video_url:
            media.append({"type": "first_clip", "url": spec.source_video_url})
        return media

    def _fill_legacy_inputs(self, input_body: dict[str, Any], spec: WanSpec) -> None:
        if spec.mode is WanMode.TEXT_TO_VIDEO:
            return
        if spec.mode in (WanMode.IMAGE_TO_VIDEO, WanMode.VIDEO_CONTINUATION):
            if not spec.image_url and not spec.source_video_url:
                raise ProviderBadRequest("image/video-conditioned Wan render has no input URL")
            if spec.image_url:
                input_body["img_url"] = spec.image_url
            if spec.source_video_url:
                input_body["video_url"] = spec.source_video_url
            return
        if spec.mode is WanMode.FIRST_LAST_FRAME:
            if not spec.first_frame_url:
                raise ProviderBadRequest("first-last-frame Wan render has no first frame")
            input_body["first_frame_url"] = spec.first_frame_url
            if spec.last_frame_url:
                input_body["last_frame_url"] = spec.last_frame_url
            return
        if spec.mode is WanMode.REFERENCE_TO_VIDEO:
            if not spec.reference_image_urls:
                raise ProviderBadRequest("reference-to-video Wan render has no reference image")
            input_body["img_url"] = spec.reference_image_urls[0]
            if len(spec.reference_image_urls) > 1:
                input_body["reference_image_urls"] = spec.reference_image_urls
            if spec.reference_voice_url:
                input_body["reference_voice_url"] = spec.reference_voice_url
            return
        if spec.mode is WanMode.INSTRUCTION_EDIT:
            if not spec.source_video_url:
                raise ProviderBadRequest("instruction-edit Wan render has no source video")
            input_body["video_url"] = spec.source_video_url

    # -- render (GATED) --------------------------------------------------- #

    async def render(self, spec: WanSpec) -> VideoResult:
        """Submit a real Wan render, poll to completion, and return the clip.

        Raises:
            LiveVideoDisabled: when ``settings.kinora_live_video`` is False.
        """
        if not self._settings.kinora_live_video:
            from .errors import LiveVideoDisabled

            raise LiveVideoDisabled(
                "live video rendering is disabled (KINORA_LIVE_VIDEO is off); "
                "no Wan task submitted",
            )

        profile = self._profile_for(spec)
        submitted = await self._submit(spec, profile)
        task_id = _get(_get(submitted, "output"), "task_id")
        task_id = task_id or _get(submitted, "task_id")
        if not task_id:
            raise ProviderError(
                "Wan submission returned no task_id",
                request_id=_get(submitted, "request_id"),
            )

        video_url = await self._poll_to_completion(str(task_id), profile.model)
        clip_bytes = await self._client.download(video_url, op="video")
        duration = float(spec.duration_s)
        self._client.record_usage(
            Usage(
                model=profile.model,
                operation="video",
                video_seconds=duration,
                request_id=str(task_id),
            )
        )
        return VideoResult(
            duration_s=duration,
            model=profile.model,
            mode=spec.mode,
            provider_task_id=str(task_id),
            clip_url=video_url,
            clip_bytes=clip_bytes,
            last_frame_bytes=None,
        )

    async def _submit(self, spec: WanSpec, profile: VideoModelProfile) -> dict[str, Any]:
        return await self._client.request_json(
            "POST",
            f"{self._client.native_base}/{_VIDEO_PATH}",
            op="video_submit",
            model=profile.model,
            json=self._submit_body(spec, profile),
            headers={"X-DashScope-Async": "enable"},
        )

    async def _poll_to_completion(self, task_id: str, model: str) -> str:
        deadline = time.monotonic() + self._poll.timeout_s
        interval = self._poll.interval_s
        while True:
            result = await self._client.request_json(
                "GET",
                f"{self._client.native_base}/tasks/{task_id}",
                op="video_poll",
                model=model,
            )
            output = _get(result, "output")
            status = str(_get(output, "task_status") or "").upper()
            if status in _TERMINAL_OK:
                url = _find_video_url(output)
                if not url:
                    raise ProviderError(
                        "Wan task succeeded but returned no video_url",
                        request_id=str(task_id),
                    )
                return url
            if status in _TERMINAL_BAD:
                raise ProviderError(
                    f"Wan task ended {status}: {_get(output, 'message') or ''}".strip(),
                    request_id=str(task_id),
                )
            if time.monotonic() >= deadline:
                from .errors import ProviderTimeout

                raise ProviderTimeout(
                    f"Wan task {task_id} did not complete within {self._poll.timeout_s}s",
                )
            await asyncio.sleep(interval)
            interval = min(self._poll.max_interval_s, interval * 1.5)

    # -- cheap verification (NO render) ----------------------------------- #

    async def verify_model_available(self, model: str | None = None) -> bool:
        """Probe whether a Wan model id is recognized by the endpoint.

        Submits an *empty-input* request: an unknown model is rejected up front
        with ``"Model not exist"`` (no task created); a recognized model returns a
        task id for a request that should fail before rendering. Some Model Studio
        regions do not support task cancellation, so deployment preflight keeps
        this behind an explicit opt-in flag.
        """
        model = model or self._settings.video_model
        try:
            body = await self._client.request_json(
                "POST",
                f"{self._client.native_base}/{_VIDEO_PATH}",
                op="video_verify",
                model=model,
                json={"model": model, "input": {}, "parameters": {}},
                headers={"X-DashScope-Async": "enable"},
            )
        except ModelNotAvailable:
            return False
        task_id = _get(_get(body, "output"), "task_id")
        if task_id:
            await self._best_effort_cancel(str(task_id))
        return True

    async def _best_effort_cancel(self, task_id: str) -> None:
        # The doomed empty-input task often fails before it can be cancelled;
        # that's fine — it never rendered, so no spend either way.
        with contextlib.suppress(ProviderError):
            await self._client.request_json(
                "POST",
                f"{self._client.native_base}/tasks/{task_id}/cancel",
                op="video_cancel",
                model="-",
            )


def _find_video_url(node: Any) -> str | None:
    """Find a provider result URL across native task response variants."""
    if isinstance(node, dict):
        for key in _VIDEO_URL_KEYS:
            value = node.get(key)
            if isinstance(value, str) and value:
                return value
        for value in node.values():
            found = _find_video_url(value)
            if found:
                return found
    elif isinstance(node, list):
        for item in node:
            found = _find_video_url(item)
            if found:
                return found
    return None


__all__ = [
    "VideoModelProfile",
    "VideoPollConfig",
    "VideoProtocol",
    "VideoProvider",
]
