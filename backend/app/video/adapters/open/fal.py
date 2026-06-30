"""``FalProvider`` — run any fal.ai-hosted video model via the queue API.

fal.ai exposes a uniform *queue* API across every model it hosts: ``POST
/{app-id}`` (or ``/{app-id}?fal_webhook=...``) enqueues a request and returns a
``request_id`` + a ``status_url`` / ``response_url``; you poll the status URL until
``COMPLETED``, then read the result. Like Replicate, that uniformity makes fal a
*meta-adapter*: one class runs Mochi, LTX-Video, HunyuanVideo, CogVideoX, or a new
fal model, parameterised by the app-id and the input field names.

Auth is ``Authorization: Key <key>`` (the fal scheme). The result video lives at
``video.url`` for most fal video apps; a fallback chain tolerates ``output.url`` /
``video_url`` variants.
"""

from __future__ import annotations

from typing import Any

from app.providers.types import WanMode, WanSpec

from .base import BaseOpenAdapter, PollConfig
from .interface import Capabilities, SubmittedTask, TaskStatus
from .jsonpath import select
from .replicate import InputMap
from .transport import OpenHttpTransport, OpenTransportConfig

__all__ = ["FalProvider"]

#: fal queue status vocabulary.
_OK = {"completed", "ok"}
_FAIL = {"failed", "error"}


class FalProvider(BaseOpenAdapter):
    """Run any fal.ai app-id as a router-ready open-model backend."""

    op_label = "video"

    def __init__(
        self,
        *,
        app_id: str,
        capabilities: Capabilities,
        transport: OpenHttpTransport,
        live_video: bool,
        input_map: InputMap | None = None,
        poll: PollConfig | None = None,
        name: str | None = None,
        usage_sink: Any | None = None,
    ) -> None:
        self._app_id = app_id.strip("/")
        self._caps = capabilities
        self._map = input_map or InputMap(
            image="image_url", first_frame="image_url", reference_image="image_url"
        )
        super().__init__(
            transport,
            live_video=live_video,
            poll=poll,
            name=name or f"fal:{self._app_id}",
            usage_sink=usage_sink,
        )

    @property
    def provider_id(self) -> str:
        return "fal"

    def capabilities(self) -> Capabilities:
        return self._caps

    def native_model(self, spec: WanSpec) -> str:
        return spec.model or self._app_id

    # -- submit --------------------------------------------------------- #

    def _submit_path(self, spec: WanSpec) -> str:
        return self._app_id

    def _build_submit_body(self, spec: WanSpec) -> dict[str, Any]:
        body: dict[str, Any] = dict(self._map.static)
        m = self._map

        def put(key: str | None, value: Any) -> None:
            if key is not None and value is not None:
                body[key] = value

        put(m.prompt, spec.prompt or None)
        if self._caps.supports_negative_prompt:
            put(m.negative_prompt, spec.negative_prompt)
        if self._caps.supports_seed:
            put(m.seed, spec.seed)
        put(m.duration, spec.duration_s if m.duration else None)
        put(m.resolution, spec.resolution if m.resolution else None)
        if spec.mode in (WanMode.IMAGE_TO_VIDEO, WanMode.VIDEO_CONTINUATION) and m.image:
            put(m.image, spec.image_url)
        elif spec.mode is WanMode.FIRST_LAST_FRAME and m.first_frame:
            put(m.first_frame, spec.first_frame_url)
        elif spec.mode is WanMode.REFERENCE_TO_VIDEO and m.reference_image:
            put(
                m.reference_image,
                spec.reference_image_urls[0] if spec.reference_image_urls else None,
            )
        return body

    def _parse_submission(self, body: dict[str, Any], model: str) -> SubmittedTask:
        request_id = select(body, "request_id || id")
        if request_id is None:
            from app.providers.errors import ProviderError

            raise ProviderError("fal queue submission returned no request_id")
        status_url = select(body, "status_url")
        return SubmittedTask(
            task_id=str(request_id),
            model=model,
            poll_url=str(status_url) if status_url else None,
            raw=body,
        )

    # -- poll ----------------------------------------------------------- #

    def _poll_path(self, task: SubmittedTask) -> str:
        return task.poll_url or f"{self._app_id}/requests/{task.task_id}/status"

    def _result_path(self, task: SubmittedTask) -> str:
        return f"{self._app_id}/requests/{task.task_id}"

    def _parse_status(self, body: dict[str, Any], task: SubmittedTask) -> TaskStatus:
        raw = str(select(body, "status") or "").lower()
        if raw in _OK:
            url = select(body, "video.url || output.url || video_url || output[-1].url")
            video_url = str(url) if isinstance(url, str) and url else None
            return TaskStatus(state=TaskStatus.SUCCEEDED, video_url=video_url, raw=body)
        if raw in _FAIL:
            return TaskStatus(
                state=TaskStatus.FAILED,
                message=str(select(body, "error || detail") or "")[:300] or None,
                raw=body,
            )
        return TaskStatus(state=TaskStatus.PENDING, raw=body)

    async def fetch(self, task: SubmittedTask, status: TaskStatus) -> bytes:
        """Fetch the clip; if the status body had no inline URL, read the result doc.

        fal's *status* endpoint sometimes omits the result payload (only the
        terminal status), with the video on the separate *result* endpoint. When
        the status carried no URL, fetch the result doc and re-extract before
        downloading — keeping the eager-download contract intact.
        """
        if status.video_url is None and status.inline_bytes is None:
            result = await self._transport.get_json(
                self._result_path(task), op="video_result", model=task.model
            )
            url = select(result, "video.url || output.url || video_url || output[-1].url")
            if isinstance(url, str) and url:
                status = TaskStatus(state=TaskStatus.SUCCEEDED, video_url=url, raw=result)
        return await super().fetch(task, status)

    # -- factory -------------------------------------------------------- #

    @classmethod
    def build(
        cls,
        *,
        app_id: str,
        capabilities: Capabilities,
        api_key: str | None,
        allow_network: bool,
        live_video: bool,
        input_map: InputMap | None = None,
        base_url: str = "https://queue.fal.run",
        poll: PollConfig | None = None,
        usage_sink: Any | None = None,
        transport: object | None = None,
        settings: Any | None = None,
    ) -> FalProvider:
        cfg = OpenTransportConfig(
            base_url=base_url,
            api_key=api_key,
            auth_scheme="key",  # fal's ``Authorization: Key <key>`` scheme
            allow_network=allow_network,
        )
        http = OpenHttpTransport(cfg, transport=transport, settings=settings)  # type: ignore[arg-type]
        return cls(
            app_id=app_id,
            capabilities=capabilities,
            transport=http,
            live_video=live_video,
            input_map=input_map,
            poll=poll,
            usage_sink=usage_sink,
        )
