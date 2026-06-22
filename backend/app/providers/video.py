"""Wan 2.7 video synthesis (async submit → poll → fetch) with a hard spend gate.

Implements the §9.3 decision-tree modes, each mapped to the right Wan model id
and request shape. The real flow is DashScope's native async video-synthesis:
submit a task, poll its status with a timeout + backoff, then download the clip.

CRITICAL: ``render`` raises :class:`LiveVideoDisabled` unless
``settings.kinora_live_video`` is set. Real Wan renders burn scarce, metered
video-seconds; degradation (Ken-Burns over a keyframe) lives in the render
worker, not here — this provider never fabricates a clip. ``verify_model_available``
confirms a model id cheaply **without** rendering.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import time
from typing import Any

from .base import ProviderClient
from .base import sdk_get as _get
from .errors import ModelNotAvailable, ProviderError
from .types import Usage, VideoResult, WanMode, WanSpec

#: Native async video-synthesis service path.
_VIDEO_PATH = "services/aigc/video-generation/video-synthesis"
_TERMINAL_OK = {"SUCCEEDED"}
_TERMINAL_BAD = {"FAILED", "CANCELED", "UNKNOWN"}


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


class VideoProvider:
    """Async Wan render client (gated) + cheap model verification."""

    def __init__(self, client: ProviderClient, *, poll: VideoPollConfig | None = None) -> None:
        self._client = client
        self._settings = client.settings
        self._poll = poll or VideoPollConfig()

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

    def _submit_kwargs(self, spec: WanSpec, model: str) -> dict[str, Any]:
        """Translate a :class:`WanSpec` into ``VideoSynthesis.async_call`` kwargs."""
        kwargs: dict[str, Any] = {
            "api_key": self._client.api_key,
            "model": model,
            "prompt": spec.prompt,
            "duration": spec.duration_s,
            "resolution": spec.resolution,
            "watermark": spec.watermark,
            "prompt_extend": spec.prompt_extend,
        }
        if spec.negative_prompt:
            kwargs["negative_prompt"] = spec.negative_prompt
        if spec.seed is not None:
            kwargs["seed"] = spec.seed

        if spec.mode is WanMode.TEXT_TO_VIDEO:
            pass
        elif spec.mode in (WanMode.IMAGE_TO_VIDEO, WanMode.VIDEO_CONTINUATION):
            kwargs["img_url"] = spec.image_url
        elif spec.mode is WanMode.FIRST_LAST_FRAME:
            kwargs["first_frame_url"] = spec.first_frame_url
            kwargs["last_frame_url"] = spec.last_frame_url
        elif spec.mode is WanMode.REFERENCE_TO_VIDEO:
            media: list[dict[str, str]] = []
            for i, url in enumerate(spec.reference_image_urls):
                item = {"type": "reference_image", "url": url}
                if i == 0 and spec.reference_voice_url:
                    item["reference_voice"] = spec.reference_voice_url
                media.append(item)
            kwargs["media"] = media
        elif spec.mode is WanMode.INSTRUCTION_EDIT and spec.source_video_url:
            kwargs["media"] = [{"type": "video", "url": spec.source_video_url}]
        return kwargs

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

        from dashscope import VideoSynthesis

        model = self._model_for(spec)
        submit = functools.partial(VideoSynthesis.async_call, **self._submit_kwargs(spec, model))
        submitted = await self._client.call_sdk(submit, op="video_submit", model=model)
        task_id = _get(_get(submitted, "output"), "task_id")
        if not task_id:
            raise ProviderError(
                "Wan submission returned no task_id",
                request_id=_get(submitted, "request_id"),
            )

        video_url = await self._poll_to_completion(VideoSynthesis, task_id, model)
        clip_bytes = await self._client.download(video_url, op="video")
        duration = float(spec.duration_s)
        self._client.record_usage(
            Usage(model=model, operation="video", video_seconds=duration, request_id=str(task_id))
        )
        return VideoResult(
            duration_s=duration,
            model=model,
            mode=spec.mode,
            provider_task_id=str(task_id),
            clip_url=video_url,
            clip_bytes=clip_bytes,
            last_frame_bytes=None,
        )

    async def _poll_to_completion(self, video_cls: Any, task_id: str, model: str) -> str:
        deadline = time.monotonic() + self._poll.timeout_s
        interval = self._poll.interval_s
        while True:
            fetch = functools.partial(video_cls.fetch, task_id, api_key=self._client.api_key)
            result = await self._client.call_sdk(fetch, op="video_poll", model=model)
            output = _get(result, "output")
            status = str(_get(output, "task_status") or "").upper()
            if status in _TERMINAL_OK:
                url = _get(output, "video_url")
                if not url:
                    raise ProviderError(
                        "Wan task succeeded but returned no video_url",
                        request_id=str(task_id),
                    )
                return str(url)
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
        """Confirm a Wan model id is recognized by the endpoint without rendering.

        Submits an *empty-input* request: an unknown model is rejected up front
        with ``"Model not exist"`` (no task created); a recognized model returns a
        task id for a request that can never render (no prompt/inputs), which we
        immediately cancel. Either way: zero video-seconds.
        """
        model = model or self._settings.video_model
        url = f"{self._client.native_base}/{_VIDEO_PATH}"
        try:
            body = await self._client.request_json(
                "POST",
                url,
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


__all__ = ["VideoPollConfig", "VideoProvider"]
