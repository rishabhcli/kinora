"""Hosted ModelScope (Alibaba open model hub) video synthesis (async submit ->
poll -> download) behind the ``KINORA_LIVE_VIDEO`` spend gate.

Mirrors the Wan ``VideoProvider`` contract (``name`` / ``render(WanSpec)`` /
``healthy()``) so it is a drop-in :class:`~app.providers.video_router.VideoBackend`,
structured like :mod:`app.providers.minimax` (gate check -> submit -> poll ->
extract the clip URL -> download -> ``record_usage``). It is the **free-tier**
video path tried before the paid MiniMax provider for the 10-book QA campaign
(see the ``modelscope_*`` settings in ``backend/app/core/config.py``). Unlike
MiniMax it carries no USD spend guard — there is nothing to charge against a
free daily quota.

::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::
::  WARNING: THE VIDEO-SPECIFIC REQUEST/RESPONSE CONTRACT IS UNCONFIRMED.  ::
::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::::

``backend/scripts/probe_modelscope_video.py`` was written to discover
ModelScope's real video-generation endpoint empirically, but no
``MODELSCOPE_API_TOKEN`` has ever been available in this environment or repo to
run it (confirmed via ``env | grep -i MODELSCOPE`` and a ``.env`` inspection) —
nobody has exercised this provider against the real API yet.

Everything below is instead modeled on ModelScope's **confirmed** (verified
2026-07-04 via web search + reading a real third-party client implementation)
async **image**-generation contract, on the assumption that video generation
follows the same async-task envelope:

    POST {base_url}/images/generations
      headers: Authorization: Bearer <token>, X-ModelScope-Async-Mode: true
      body:    {"model": ..., "prompt": ..., ...}
      ->       {"task_id": "..."}
    GET {base_url}/tasks/{task_id}
      headers: Authorization: Bearer <token>
      ->       {"task_status": "SUCCEED" | "FAILED" | ..., "output_images": [url, ...]}

Concretely UNCONFIRMED for *video* (each is this module's best guess by
analogy, not a verified fact — grep this file for "UNCONFIRMED" to find every
one of them):

* The endpoint path itself: ``videos/generations`` below is a guess by analogy
  with ``images/generations``. The probe script's candidate list also tries
  ``video/generations`` as a second guess. Either could 404 or not exist at all.
* The response field ``output_videos``: the confirmed image contract's field is
  ``output_images`` (a list of URLs, first element used); ``output_videos`` is
  this module's guess at its video analog. It could just as easily be named
  ``output_video_url``, ``videos``, ``output_urls``, etc.
* That ``model="Wan-AI/Wan2.2-T2V-A14B"`` (``modelscope_video_model`` in
  config) is a valid ModelScope inference-API model id for this endpoint at all.
* Whether the video endpoint needs any header beyond ``X-ModelScope-Async-Mode``
  — the confirmed image contract also sends an ``X-ModelScope-Task-Type``
  header on both submit and poll; this module does not replicate it, since a
  correct value for video is unknown and inventing one would be a silent guess
  rather than a flagged one.
* The exact ``task_status`` terminal strings: ``SUCCEED``/``FAILED`` are
  confirmed for the image endpoint's response and are assumed identical here.

This module only supports :attr:`~app.providers.types.WanMode.TEXT_TO_VIDEO`
(matching the configured default model, a T2V-only id) and sends the minimal
documented body shape (``model`` + ``prompt`` only) rather than guessing at
additional fields (``size``/``steps``/``guidance``/``seed``/``negative_prompt``)
that the *image* contract accepts but that may not apply, or may be named
differently, on the video endpoint.

Once a real ``MODELSCOPE_API_TOKEN`` is available, re-run
``backend/scripts/probe_modelscope_video.py`` and fix any of the above that
turn out to be wrong.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from app.core.config import Settings

from .base import ProviderClient, UsageSink
from .base import sdk_get as _get
from .errors import ProviderError
from .types import Usage, VideoResult, WanMode, WanSpec

#: REST paths under ``{modelscope_base_url}`` (e.g. https://api-inference.modelscope.cn/v1).
#: UNCONFIRMED — see the module docstring.
_SUBMIT_PATH = "videos/generations"
_POLL_PATH = "tasks"

#: Marks the submit call as an async job (confirmed for the image-generation
#: analog; assumed to carry over to video unchanged).
_ASYNC_MODE_HEADERS = {"X-ModelScope-Async-Mode": "true"}

#: ``task_status`` terminal values, confirmed for the image-generation analog.
#: UNCONFIRMED whether the video endpoint uses the same strings.
_STATUS_OK = "SUCCEED"
_STATUS_FAIL = "FAILED"


class ModelScopeVideoProvider:
    """Hosted ModelScope (free-tier) render client.

    Satisfies the :class:`~app.providers.video_router.VideoBackend` protocol
    (``name`` / ``render`` / ``healthy``) so it is a drop-in alternative to the
    Wan :class:`~app.providers.video.VideoProvider` and to
    :class:`~app.providers.minimax.MiniMaxVideoProvider`.

    See the module docstring for the prominent caveat: the video-specific
    request/response contract implemented here is UNCONFIRMED.
    """

    def __init__(
        self,
        client: ProviderClient,
        *,
        name: str | None = None,
        poll_interval_s: float = 10.0,
        poll_timeout_s: float = 600.0,
    ) -> None:
        self._client = client
        self._settings = client.settings
        self._poll_interval_s = poll_interval_s
        self._poll_timeout_s = poll_timeout_s
        self.name = name or f"modelscope:{self._settings.modelscope_video_model}"

    # -- liveness (no render spend) -------------------------------------- #

    async def healthy(self) -> bool:
        """Cheap probe: no network when the live gate is off (gate != fault)."""
        return True

    # -- request shape --------------------------------------------------- #

    def _submit_body(self, spec: WanSpec) -> dict[str, Any]:
        """Translate a :class:`WanSpec` into the ModelScope submit JSON.

        UNCONFIRMED (see module docstring): only ``model``/``prompt`` are sent.
        The configured default model is text-to-video only, and there is no
        confirmed image-conditioning field name for this endpoint, so any other
        :class:`WanMode` is rejected rather than silently dropping the request's
        image inputs on the floor.
        """
        if spec.mode is not WanMode.TEXT_TO_VIDEO:
            from .errors import ProviderBadRequest

            raise ProviderBadRequest(
                "ModelScope video provider only supports text_to_video today "
                "(the unconfirmed video contract has no known image-conditioning "
                f"field); got {spec.mode.value}"
            )
        return {
            "model": self._settings.modelscope_video_model,
            "prompt": spec.prompt or "",
        }

    @staticmethod
    def _map_status(status: str) -> str:
        if status == _STATUS_OK:
            return "ok"
        if status == _STATUS_FAIL:
            return "fail"
        return "pending"

    @staticmethod
    def _extract_download_url(result: dict[str, Any]) -> str | None:
        """Pull the clip URL out of a terminal poll response.

        UNCONFIRMED (see module docstring): ``output_videos`` is this module's
        guess at the video analog of the confirmed image contract's
        ``output_images`` — a list of URLs, first element used.
        """
        videos = _get(result, "output_videos")
        if isinstance(videos, list) and videos:
            return str(videos[0])
        return None

    # -- render (GATED) ---------------------------------------------------- #

    async def render(self, spec: WanSpec) -> VideoResult:
        """Submit a real ModelScope render, poll, download, and return it.

        Only guard: :class:`~app.providers.errors.LiveVideoDisabled` when
        ``kinora_live_video`` is off (no network). Unlike MiniMax there is no
        USD ceiling guard — ModelScope is the free-tier path.
        """
        if not self._settings.kinora_live_video:
            from .errors import LiveVideoDisabled

            raise LiveVideoDisabled(
                "live video rendering is disabled (KINORA_LIVE_VIDEO is off); "
                "no ModelScope task submitted",
            )

        task_id = await self._submit(spec)
        download_url = await self._poll_to_completion(task_id)
        clip_bytes = await self._client.download(download_url, op="video")

        duration = float(spec.duration_s)
        self._client.record_usage(
            Usage(
                model=self._settings.modelscope_video_model,
                operation="video",
                video_seconds=duration,
                request_id=task_id,
            )
        )
        return VideoResult(
            duration_s=duration,
            model=self._settings.modelscope_video_model,
            mode=spec.mode,
            provider_task_id=task_id,
            clip_url=download_url,
            clip_bytes=clip_bytes,
            last_frame_bytes=None,
        )

    async def _submit(self, spec: WanSpec) -> str:
        body = self._submit_body(spec)
        result = await self._client.request_json(
            "POST",
            f"{self._client.base_url}/{_SUBMIT_PATH}",
            op="modelscope_video_submit",
            model=self._settings.modelscope_video_model,
            json=body,
            headers=_ASYNC_MODE_HEADERS,
        )
        task_id = _get(result, "task_id")
        if not task_id:
            raise ProviderError("ModelScope submission returned no task_id")
        return str(task_id)

    async def _poll_to_completion(self, task_id: str) -> str:
        deadline = time.monotonic() + self._poll_timeout_s
        while True:
            result = await self._client.request_json(
                "GET",
                f"{self._client.base_url}/{_POLL_PATH}/{task_id}",
                op="modelscope_video_poll",
                model=self._settings.modelscope_video_model,
            )
            mapped = self._map_status(str(_get(result, "task_status") or ""))
            if mapped == "ok":
                url = self._extract_download_url(result)
                if not url:
                    raise ProviderError(
                        f"ModelScope task {task_id} succeeded but returned no "
                        "output_videos",
                        request_id=task_id,
                    )
                return url
            if mapped == "fail":
                raise ProviderError(
                    f"ModelScope task {task_id} ended FAILED", request_id=task_id
                )
            if time.monotonic() >= deadline:
                from .errors import ProviderTimeout

                raise ProviderTimeout(
                    f"ModelScope task {task_id} did not complete within "
                    f"{self._poll_timeout_s}s",
                )
            await asyncio.sleep(self._poll_interval_s)


def build_modelscope_video_provider(
    settings: Settings,
    *,
    usage_sink: UsageSink | None = None,
) -> ModelScopeVideoProvider:
    """Build a ModelScope video backend on its own ModelScope-configured client.

    Mirrors :func:`app.providers.build_minimax_video_provider`'s shape: uses
    ``base_url_override`` + ``api_key_override`` so the provider talks to the
    ModelScope host under its own key while still reusing the shared
    :class:`ProviderClient` resilience (retries/breaker/rate-limit) and the
    caller's usage sink (unified cost/budget accounting). Unlike MiniMax, no
    spend store is wired here — ModelScope is the free-tier path and carries no
    USD guard (see the module docstring).
    """
    ms_client = ProviderClient(
        settings,
        usage_sink=usage_sink,
        base_url_override=settings.modelscope_base_url,
        api_key_override=settings.modelscope_api_key,
    )
    return ModelScopeVideoProvider(ms_client)


__all__ = ["ModelScopeVideoProvider", "build_modelscope_video_provider"]
