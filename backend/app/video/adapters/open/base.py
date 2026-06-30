"""``BaseOpenAdapter``: the shared submit → poll → fetch → last-frame lifecycle.

Every concrete open-model adapter (Stability SVD, Mochi, CogVideoX, LTX-Video,
HunyuanVideo) and every meta-adapter (Replicate, fal, ComfyUI / OpenAPI) subclasses
this. The base owns the parts that are *identical everywhere*:

* the **spend gate** — ``render`` raises :class:`LiveVideoDisabled` before any
  network call when ``KINORA_LIVE_VIDEO`` is off (sacred, never a health fault);
* a **capability pre-check** — a :class:`WanSpec` the model can't satisfy is
  rejected with a precise :class:`ProviderBadRequest` *before* submitting;
* the **poll loop** — bounded timeout + capped exponential backoff over the
  adapter's normalized :class:`TaskStatus`;
* the **eager download** — provider URLs expire, so clip bytes are fetched
  immediately (or taken inline);
* **last-frame extraction** for continuity (best-effort, ffmpeg-optional);
* **usage accounting** — one :class:`Usage` per render into the shared sink.

Subclasses implement only the provider-specific surface: ``capabilities``,
``_build_submit_body`` / ``_parse_submission`` (or ``submit``), ``_poll_url`` /
``_parse_status`` (or ``poll``), and ``_status_video_url`` (or ``fetch``).
"""

from __future__ import annotations

import asyncio
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from app.core.logging import get_logger
from app.providers.errors import (
    LiveVideoDisabled,
    ProviderBadRequest,
    ProviderError,
    ProviderTimeout,
)
from app.providers.types import Usage, VideoResult, WanSpec

from .interface import Capabilities, SubmittedTask, TaskStatus
from .lastframe import extract_last_frame
from .transport import OpenHttpTransport

logger = get_logger("app.video.adapters.open.base")

__all__ = ["BaseOpenAdapter", "PollConfig"]


@dataclass(frozen=True, slots=True)
class PollConfig:
    """Polling bounds for an async open-model task (no env reads)."""

    timeout_s: float = 600.0
    interval_s: float = 3.0
    max_interval_s: float = 15.0
    backoff: float = 1.5


class BaseOpenAdapter(ABC):
    """Common lifecycle for an open / self-hosted / gateway video adapter.

    Implements the router-facing :class:`~.interface.OpenVideoBackend` contract
    (``name`` / ``render`` / ``healthy`` / ``capabilities``) on top of the
    fine-grained :class:`~.interface.SubmitPollFetch` lifecycle, which subclasses
    fill in.
    """

    #: Coarse op label for usage/telemetry; subclasses may override.
    op_label: str = "video"

    def __init__(
        self,
        transport: OpenHttpTransport,
        *,
        live_video: bool,
        poll: PollConfig | None = None,
        name: str | None = None,
        usage_sink: Any | None = None,
    ) -> None:
        self._transport = transport
        self._live_video = bool(live_video)
        self._poll = poll or PollConfig()
        self._usage_sink = usage_sink
        self.name = name or f"open:{self.provider_id}"

    # -- identity / capabilities (subclass) ------------------------------- #

    @property
    @abstractmethod
    def provider_id(self) -> str:
        """A short stable provider family id (``"stability"``, ``"replicate"``, ...)."""

    @abstractmethod
    def capabilities(self) -> Capabilities:
        """The static capability profile for this backend."""

    @abstractmethod
    def native_model(self, spec: WanSpec) -> str:
        """Resolve the native model id this ``spec`` renders against."""

    # -- submit (subclass) ------------------------------------------------ #

    @abstractmethod
    def _build_submit_body(self, spec: WanSpec) -> dict[str, Any]:
        """Translate a canonical :class:`WanSpec` into the provider's native payload."""

    @abstractmethod
    def _submit_path(self, spec: WanSpec) -> str:
        """The endpoint path (or absolute URL) a submission POSTs to."""

    @abstractmethod
    def _parse_submission(self, body: dict[str, Any], model: str) -> SubmittedTask:
        """Extract a :class:`SubmittedTask` from a submission response body."""

    # -- poll / fetch (subclass) ------------------------------------------ #

    @abstractmethod
    def _poll_path(self, task: SubmittedTask) -> str:
        """The status endpoint path (or absolute URL) for ``task``."""

    @abstractmethod
    def _parse_status(self, body: dict[str, Any], task: SubmittedTask) -> TaskStatus:
        """Normalize a status response body into a :class:`TaskStatus`."""

    # -- composite lifecycle (base owns these) ---------------------------- #

    async def submit(self, spec: WanSpec) -> SubmittedTask:
        """Submit a render and return its poll handle (gate + capability checked)."""
        self._enforce_gate()
        self._enforce_capabilities(spec)
        model = self.native_model(spec)
        body = self._build_submit_body(spec)
        resp = await self._transport.post_json(
            self._submit_path(spec),
            op=f"{self.op_label}_submit",
            model=model,
            body=body,
        )
        task = self._parse_submission(resp, model)
        logger.info(
            "video.open.submitted",
            provider=self.provider_id,
            backend=self.name,
            model=model,
            task_id=task.task_id,
        )
        return task

    async def poll(self, task: SubmittedTask) -> TaskStatus:
        """Poll one status tick for ``task`` and normalize it."""
        body = await self._transport.get_json(
            self._poll_path(task),
            op=f"{self.op_label}_poll",
            model=task.model,
        )
        return self._parse_status(body, task)

    async def fetch(self, task: SubmittedTask, status: TaskStatus) -> bytes:
        """Eagerly download (or take inline) the finished clip bytes."""
        if status.inline_bytes is not None:
            return status.inline_bytes
        if not status.video_url:
            raise ProviderError(
                f"{self.provider_id} task succeeded but exposed no video url",
                request_id=task.task_id,
            )
        return await self._transport.download(status.video_url, op=self.op_label)

    async def render(self, spec: WanSpec) -> VideoResult:
        """Run the full submit → poll → fetch → last-frame lifecycle.

        Raises:
            LiveVideoDisabled: when ``KINORA_LIVE_VIDEO`` is off (before any call).
            ProviderBadRequest: when ``spec`` is outside this backend's capabilities.
            ProviderTimeout: when the task does not finish within the poll window.
            ProviderError: on a terminal task failure or a transport fault.
        """
        task = await self.submit(spec)
        status = await self._poll_to_completion(task)
        clip_bytes = await self.fetch(task, status)
        last_frame = extract_last_frame(clip_bytes)
        duration = float(spec.duration_s)
        self._record_usage(task.model, duration, task.task_id)
        logger.info(
            "video.open.rendered",
            provider=self.provider_id,
            backend=self.name,
            model=task.model,
            task_id=task.task_id,
            bytes=len(clip_bytes),
            last_frame=last_frame is not None,
        )
        return VideoResult(
            duration_s=duration,
            model=task.model,
            mode=spec.mode,
            provider_task_id=task.task_id,
            clip_url=status.video_url,
            clip_bytes=clip_bytes,
            last_frame_bytes=last_frame,
        )

    async def _poll_to_completion(self, task: SubmittedTask) -> TaskStatus:
        deadline = time.monotonic() + self._poll.timeout_s
        interval = self._poll.interval_s
        while True:
            status = await self.poll(task)
            if status.state == TaskStatus.SUCCEEDED:
                return status
            if status.state == TaskStatus.FAILED:
                raise ProviderError(
                    f"{self.provider_id} task ended FAILED: {status.message or ''}".strip(),
                    request_id=task.task_id,
                )
            if time.monotonic() >= deadline:
                raise ProviderTimeout(
                    f"{self.provider_id} task {task.task_id} did not complete within "
                    f"{self._poll.timeout_s}s",
                )
            await asyncio.sleep(interval)
            interval = min(self._poll.max_interval_s, interval * self._poll.backoff)

    async def healthy(self) -> bool:
        """Cheap liveness probe (no render spend).

        With the spend gate off, returns ``True`` without any network call — a
        gated-off backend is still *routable* (the gate, not the backend, blocks
        the render). With the gate on, defers to :meth:`_probe` (overridable; by
        default a no-network ``True`` so a missing probe never wedges the router).
        """
        if not self._live_video:
            return True
        try:
            return await self._probe()
        except ProviderError:
            return False

    async def _probe(self) -> bool:
        """Optional live health probe (no render). Default: assume healthy."""
        return True

    # -- gate + capability enforcement ------------------------------------ #

    def _enforce_gate(self) -> None:
        if not self._live_video:
            raise LiveVideoDisabled(
                "live video rendering is disabled (KINORA_LIVE_VIDEO is off); "
                f"no {self.provider_id} task submitted",
            )

    def _enforce_capabilities(self, spec: WanSpec) -> None:
        reasons = self.capabilities().reasons_unsupported(spec)
        if reasons:
            raise ProviderBadRequest(
                f"{self.provider_id} cannot render this spec: " + "; ".join(reasons),
            )

    def _record_usage(self, model: str, duration_s: float, task_id: str) -> None:
        usage = Usage(
            model=model,
            operation=self.op_label,
            video_seconds=0.0 if self.capabilities().self_hosted else duration_s,
            request_id=task_id,
        )
        sink = self._usage_sink
        if sink is None:
            return
        try:
            sink(usage)
        except Exception:  # noqa: BLE001 - a broken sink must never fail a render
            logger.warning("video.open.usage_sink_error", provider=self.provider_id)

    async def aclose(self) -> None:
        await self._transport.aclose()
