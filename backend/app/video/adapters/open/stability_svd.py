"""Stability **Stable Video Diffusion** — image-to-video over Stability's own API.

Stability's SVD endpoint is *natively* image-conditioned: ``POST
/v2beta/image-to-video`` with the start image returns a ``generation`` id you poll
at ``/v2beta/image-to-video/result/{id}`` until the clip is ready. SVD only does
**image-to-video** (no text-only, no first-last, no reference set) and produces a
short clip (~2-4s) at 24fps, so its :class:`Capabilities` are deliberately narrow —
the capability pre-check rejects a t2v shot before any spend.

The result endpoint returns the mp4 bytes directly (``video`` base64 or a binary
body) rather than a URL, which the base lifecycle handles via inline bytes.
"""

from __future__ import annotations

from typing import Any

from app.providers.types import WanMode, WanSpec

from .base import BaseOpenAdapter, PollConfig
from .descriptor import _decode_inline_b64
from .interface import Capabilities, SubmittedTask, TaskStatus
from .jsonpath import select
from .transport import OpenHttpTransport, OpenTransportConfig

__all__ = ["StableVideoDiffusionProvider", "svd_capabilities"]


def svd_capabilities(name: str = "stability-svd") -> Capabilities:
    """The canonical capability profile for Stable Video Diffusion."""
    return Capabilities(
        name=name,
        modes=frozenset({WanMode.IMAGE_TO_VIDEO}),
        max_duration_s=4.0,
        min_duration_s=1.0,
        resolutions=frozenset({"576P", "720P", "1024x576"}),
        supports_seed=True,
        supports_negative_prompt=False,
        supports_audio=False,
        max_reference_images=0,
        default_fps=24,
        cost_per_s=0.6,
        quality=0.55,
    )


class StableVideoDiffusionProvider(BaseOpenAdapter):
    """Stability AI's image-to-video model over its native v2beta API."""

    op_label = "video"

    def __init__(
        self,
        transport: OpenHttpTransport,
        *,
        live_video: bool,
        model_id: str = "stable-video-diffusion",
        poll: PollConfig | None = None,
        name: str | None = None,
        usage_sink: Any | None = None,
    ) -> None:
        self._model_id = model_id
        self._caps = svd_capabilities(name or "stability-svd")
        super().__init__(
            transport,
            live_video=live_video,
            poll=poll,
            name=name or "stability-svd",
            usage_sink=usage_sink,
        )

    @property
    def provider_id(self) -> str:
        return "stability"

    def capabilities(self) -> Capabilities:
        return self._caps

    def native_model(self, spec: WanSpec) -> str:
        return spec.model or self._model_id

    def _submit_path(self, spec: WanSpec) -> str:
        return "v2beta/image-to-video"

    def _build_submit_body(self, spec: WanSpec) -> dict[str, Any]:
        body: dict[str, Any] = {"image": spec.image_url}
        if spec.seed is not None:
            body["seed"] = spec.seed
        # SVD exposes motion + cfg knobs; expose conservative, deterministic values.
        body["cfg_scale"] = 1.8
        body["motion_bucket_id"] = 127
        return body

    def _parse_submission(self, body: dict[str, Any], model: str) -> SubmittedTask:
        gen_id = select(body, "id || generation_id || generationId")
        if gen_id is None:
            from app.providers.errors import ProviderError

            raise ProviderError("stability image-to-video returned no generation id")
        return SubmittedTask(task_id=str(gen_id), model=model, raw=body)

    def _poll_path(self, task: SubmittedTask) -> str:
        return f"v2beta/image-to-video/result/{task.task_id}"

    def _parse_status(self, body: dict[str, Any], task: SubmittedTask) -> TaskStatus:
        status = str(select(body, "status || finish_reason") or "").lower()
        # An ``in-progress`` body carries only a status; a finished body carries the
        # ``video`` base64 (or ``errors``).
        errors = select(body, "errors || error")
        if errors:
            return TaskStatus(state=TaskStatus.FAILED, message=str(errors)[:300], raw=body)
        inline = _decode_inline_b64(select(body, "video"))
        if inline is not None:
            return TaskStatus(state=TaskStatus.SUCCEEDED, inline_bytes=inline, raw=body)
        if status in ("succeeded", "complete", "success"):
            url = select(body, "video_url || url")
            return TaskStatus(
                state=TaskStatus.SUCCEEDED,
                video_url=str(url) if isinstance(url, str) and url else None,
                raw=body,
            )
        return TaskStatus(state=TaskStatus.PENDING, raw=body)

    @classmethod
    def build(
        cls,
        *,
        api_key: str | None,
        allow_network: bool,
        live_video: bool,
        base_url: str = "https://api.stability.ai",
        poll: PollConfig | None = None,
        usage_sink: Any | None = None,
        transport: object | None = None,
        settings: Any | None = None,
    ) -> StableVideoDiffusionProvider:
        cfg = OpenTransportConfig(
            base_url=base_url,
            api_key=api_key,
            auth_scheme="bearer",
            allow_network=allow_network,
            extra_headers={"accept": "application/json"},
        )
        http = OpenHttpTransport(cfg, transport=transport, settings=settings)  # type: ignore[arg-type]
        return cls(http, live_video=live_video, poll=poll, usage_sink=usage_sink)
