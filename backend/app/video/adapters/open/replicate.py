"""``ReplicateProvider`` — run *any* Replicate model by ``owner/model:version``.

Replicate's prediction API is uniform across every model it hosts: ``POST
/v1/predictions`` with ``{"version": "<64-hex>", "input": {...}}`` returns a
prediction whose ``status`` you poll at ``urls.get`` until ``succeeded``, then read
the video URL from ``output``. That uniformity makes Replicate a *meta-adapter*:
one class can run Stability SVD, Mochi, CogVideoX, LTX-Video, HunyuanVideo, or a
model that ships next week — the only per-model knowledge is the version hash and
the input field names, both supplied as config.

This adapter therefore takes an :class:`InputMap` (canonical → native field names)
so the same code drives any model's ``input`` object. Open-model defaults for the
common field names are baked in; override per model as needed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.providers.types import WanMode, WanSpec

from .base import BaseOpenAdapter, PollConfig
from .interface import Capabilities, SubmittedTask, TaskStatus
from .jsonpath import select
from .transport import OpenHttpTransport, OpenTransportConfig

__all__ = ["InputMap", "ReplicateProvider"]


@dataclass(frozen=True, slots=True)
class InputMap:
    """Maps canonical request fields onto a Replicate model's ``input`` keys.

    Different models name the same concept differently (``image`` vs ``input_image``
    vs ``first_frame_image``). Supplying a map per model lets one adapter run them
    all. ``None`` means "this model has no such field" → the value is omitted.
    """

    prompt: str | None = "prompt"
    negative_prompt: str | None = "negative_prompt"
    image: str | None = "image"  # i2v / continuation driving frame
    first_frame: str | None = None
    last_frame: str | None = None
    reference_image: str | None = None
    seed: str | None = "seed"
    duration: str | None = None
    fps: str | None = None
    num_frames: str | None = None
    resolution: str | None = None
    #: Static input fields merged verbatim into every request.
    static: dict[str, Any] = field(default_factory=dict)


class ReplicateProvider(BaseOpenAdapter):
    """Run any ``owner/model:version`` on Replicate as a router-ready backend."""

    op_label = "video"

    def __init__(
        self,
        *,
        version: str,
        capabilities: Capabilities,
        transport: OpenHttpTransport,
        live_video: bool,
        input_map: InputMap | None = None,
        poll: PollConfig | None = None,
        name: str | None = None,
        usage_sink: Any | None = None,
        model_label: str = "replicate-model",
    ) -> None:
        self._version = version
        self._caps = capabilities
        self._map = input_map or InputMap()
        self._model_label = model_label
        super().__init__(
            transport,
            live_video=live_video,
            poll=poll,
            name=name or f"replicate:{model_label}",
            usage_sink=usage_sink,
        )

    @property
    def provider_id(self) -> str:
        return "replicate"

    def capabilities(self) -> Capabilities:
        return self._caps

    def native_model(self, spec: WanSpec) -> str:
        return spec.model or self._model_label

    # -- submit --------------------------------------------------------- #

    def _submit_path(self, spec: WanSpec) -> str:
        return "predictions"

    def _build_submit_body(self, spec: WanSpec) -> dict[str, Any]:
        inputs: dict[str, Any] = dict(self._map.static)
        m = self._map

        def put(key: str | None, value: Any) -> None:
            if key is not None and value is not None:
                inputs[key] = value

        put(m.prompt, spec.prompt or None)
        if self._caps.supports_negative_prompt:
            put(m.negative_prompt, spec.negative_prompt)
        if self._caps.supports_seed:
            put(m.seed, spec.seed)
        put(m.duration, spec.duration_s if m.duration else None)
        put(m.resolution, spec.resolution if m.resolution else None)
        if m.fps:
            put(m.fps, self._caps.default_fps)
        if m.num_frames:
            put(m.num_frames, int(round(spec.duration_s * self._caps.default_fps)))

        self._apply_conditioning(spec, inputs, m)
        return {"version": self._version, "input": inputs}

    @staticmethod
    def _apply_conditioning(spec: WanSpec, inputs: dict[str, Any], m: InputMap) -> None:
        if spec.mode in (WanMode.IMAGE_TO_VIDEO, WanMode.VIDEO_CONTINUATION):
            if m.image and spec.image_url:
                inputs[m.image] = spec.image_url
        elif spec.mode is WanMode.FIRST_LAST_FRAME:
            if m.first_frame and spec.first_frame_url:
                inputs[m.first_frame] = spec.first_frame_url
            if m.last_frame and spec.last_frame_url:
                inputs[m.last_frame] = spec.last_frame_url
        elif (
            spec.mode is WanMode.REFERENCE_TO_VIDEO
            and m.reference_image
            and spec.reference_image_urls
        ):
            inputs[m.reference_image] = spec.reference_image_urls[0]

    def _parse_submission(self, body: dict[str, Any], model: str) -> SubmittedTask:
        task_id = select(body, "id")
        if task_id is None:
            from app.providers.errors import ProviderError

            raise ProviderError("replicate prediction returned no id")
        poll_url = select(body, "urls.get")
        return SubmittedTask(
            task_id=str(task_id),
            model=model,
            poll_url=str(poll_url) if poll_url else None,
            raw=body,
        )

    # -- poll ----------------------------------------------------------- #

    def _poll_path(self, task: SubmittedTask) -> str:
        return task.poll_url or f"predictions/{task.task_id}"

    def _parse_status(self, body: dict[str, Any], task: SubmittedTask) -> TaskStatus:
        raw = str(select(body, "status") or "").lower()
        if raw == "succeeded":
            url = select(body, "output[-1] || output || output.url")
            video_url = str(url) if isinstance(url, str) and url else None
            return TaskStatus(state=TaskStatus.SUCCEEDED, video_url=video_url, raw=body)
        if raw in ("failed", "canceled", "cancelled"):
            return TaskStatus(
                state=TaskStatus.FAILED,
                message=str(select(body, "error") or "")[:300] or None,
                raw=body,
            )
        return TaskStatus(state=TaskStatus.PENDING, raw=body)

    # -- factory -------------------------------------------------------- #

    @classmethod
    def build(
        cls,
        *,
        version: str,
        capabilities: Capabilities,
        api_key: str | None,
        allow_network: bool,
        live_video: bool,
        input_map: InputMap | None = None,
        base_url: str = "https://api.replicate.com/v1",
        poll: PollConfig | None = None,
        usage_sink: Any | None = None,
        model_label: str = "replicate-model",
        transport: object | None = None,
        settings: Any | None = None,
    ) -> ReplicateProvider:
        cfg = OpenTransportConfig(
            base_url=base_url,
            api_key=api_key,
            auth_scheme="token",  # Replicate's classic ``Authorization: Token`` scheme
            allow_network=allow_network,
        )
        http = OpenHttpTransport(cfg, transport=transport, settings=settings)  # type: ignore[arg-type]
        return cls(
            version=version,
            capabilities=capabilities,
            transport=http,
            live_video=live_video,
            input_map=input_map,
            poll=poll,
            usage_sink=usage_sink,
            model_label=model_label,
        )
