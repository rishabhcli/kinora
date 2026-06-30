"""The reference :class:`EchoVideoProvider` — a deterministic, no-network fake.

Echo is the canonical example implementation of
:class:`~app.video.abstraction.provider.UniversalVideoProvider` and the fixture
every other video subsystem test routes through. It NEVER touches the network and
NEVER spends provider credits, so it is safe to drive at full speed in CI with
``KINORA_LIVE_VIDEO`` off — it is not a real model and burns zero video-seconds.

Behaviour (fully deterministic):

* :meth:`submit` returns a handle whose ``task_id`` is derived from the request's
  :meth:`~app.video.abstraction.schema.CanonicalVideoRequest.idempotency_key`, so
  the same request always yields the same task id.
* By default it can run **async** (submit→poll, ``poll_steps`` polls to terminal)
  or **synchronous** (terminal at submit with an inline result), controlled by the
  declared :class:`~app.video.abstraction.capability.SubmitStyle`.
* The produced clip bytes are a deterministic function of the idempotency key, so
  two identical renders are byte-identical (handy for cache/dedupe assertions).
* It can be scripted to *fail* a given task id (terminal FAILED) to exercise the
  error path without any real provider.

This makes Echo a precise stand-in for a hosted model when testing the registry,
normalizer round-trips, and the ``submit→poll→fetch`` lifecycle.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from app.core.logging import get_logger

from .capability import (
    ReferenceStyle,
    SubmitStyle,
    VideoCapability,
    VideoMode,
)
from .provider import BaseVideoProvider, VideoProviderError
from .schema import (
    CanonicalVideoRequest,
    CanonicalVideoResult,
    TaskState,
    VideoTaskHandle,
)

logger = get_logger("app.video.abstraction.echo")

_ALL_MODES: frozenset[VideoMode] = frozenset(VideoMode)


def default_echo_capability(provider_id: str = "echo") -> VideoCapability:
    """A permissive capability covering every mode (the Echo default envelope)."""
    return VideoCapability(
        provider_id=provider_id,
        display_name="Echo (deterministic fake)",
        modes=_ALL_MODES,
        min_duration_s=1.0,
        max_duration_s=15.0,
        resolutions=("480P", "720P", "1080P"),
        aspect_ratios=("16:9", "9:16", "1:1"),
        fps_options=(16, 24, 30),
        default_resolution="720P",
        default_aspect_ratio="16:9",
        default_fps=24,
        supports_seed=True,
        supports_negative_prompt=True,
        reference_style=ReferenceStyle.TYPED_MEDIA,
        max_reference_images=4,
        supports_audio=False,
        max_prompt_chars=2000,
        submit_style=SubmitStyle.ASYNC_POLL,
        supports_cancel=True,
        tags=frozenset({"fake", "deterministic"}),
    )


@dataclass
class _EchoTask:
    """Internal per-task bookkeeping for the Echo provider."""

    request: CanonicalVideoRequest
    state: TaskState
    polls_remaining: int


class EchoVideoProvider(BaseVideoProvider):
    """A deterministic, network-free :class:`UniversalVideoProvider` reference.

    Args:
        capability: the declared envelope; defaults to :func:`default_echo_capability`.
            Its ``submit_style`` decides async-vs-sync behaviour.
        poll_steps: number of ``poll`` calls before an async task reaches
            ``SUCCEEDED`` (0 == terminal at the first poll).
        fail_keys: idempotency keys (or ``shot_id`` s) whose task ends FAILED — for
            exercising the error path deterministically.
    """

    def __init__(
        self,
        capability: VideoCapability | None = None,
        *,
        poll_steps: int = 1,
        fail_keys: frozenset[str] = frozenset(),
    ) -> None:
        self._capability = capability or default_echo_capability()
        self.provider_id = self._capability.provider_id
        self._poll_steps = max(0, poll_steps)
        self._fail_keys = frozenset(fail_keys)
        self._tasks: dict[str, _EchoTask] = {}
        #: Observable call counts so tests can assert the lifecycle was driven.
        self.submit_calls = 0
        self.poll_calls = 0
        self.fetch_calls = 0
        self.cancel_calls = 0

    def capabilities(self) -> VideoCapability:
        return self._capability

    # -- lifecycle -------------------------------------------------------- #

    async def submit(self, request: CanonicalVideoRequest) -> VideoTaskHandle:
        self.submit_calls += 1
        self._validate_against_capability(request)
        key = request.idempotency_key()
        task_id = f"echo-{key[:16]}"
        should_fail = key in self._fail_keys or (
            request.shot_id is not None and request.shot_id in self._fail_keys
        )
        is_sync = self._capability.submit_style is SubmitStyle.SYNCHRONOUS

        if should_fail:
            state = TaskState.FAILED
        elif is_sync or self._poll_steps == 0:
            state = TaskState.SUCCEEDED
        else:
            state = TaskState.RUNNING
        self._tasks[task_id] = _EchoTask(
            request=request,
            state=state,
            polls_remaining=0 if state.is_terminal else self._poll_steps,
        )
        inline = (
            self._build_result(task_id, request)
            if state is TaskState.SUCCEEDED
            else None
        )
        logger.info("echo.submit", task_id=task_id, state=state.value, shot_id=request.shot_id)
        return VideoTaskHandle(
            provider_id=self.provider_id,
            task_id=task_id,
            state=state,
            shot_id=request.shot_id,
            inline_result=inline,
        )

    async def poll(self, handle: VideoTaskHandle) -> VideoTaskHandle:
        self.poll_calls += 1
        task = self._task(handle)
        if not task.state.is_terminal:
            if task.polls_remaining > 0:
                task.polls_remaining -= 1
            if task.polls_remaining <= 0:
                task.state = TaskState.SUCCEEDED
        inline = (
            self._build_result(handle.task_id, task.request)
            if task.state is TaskState.SUCCEEDED
            else None
        )
        return handle.model_copy(update={"state": task.state, "inline_result": inline})

    async def fetch(self, handle: VideoTaskHandle) -> CanonicalVideoResult:
        self.fetch_calls += 1
        task = self._task(handle)
        if task.state is not TaskState.SUCCEEDED:
            raise VideoProviderError(
                f"cannot fetch task {handle.task_id} in state {task.state.value}",
                provider_id=self.provider_id,
                task_id=handle.task_id,
                retryable=task.state is not TaskState.CANCELED,
            )
        return self._build_result(handle.task_id, task.request)

    async def cancel(self, handle: VideoTaskHandle) -> VideoTaskHandle:
        self.cancel_calls += 1
        if not self._capability.supports_cancel:
            raise VideoProviderError(
                f"provider {self.provider_id} does not support cancel",
                provider_id=self.provider_id,
                task_id=handle.task_id,
            )
        task = self._tasks.get(handle.task_id)
        if task is not None and not task.state.is_terminal:
            task.state = TaskState.CANCELED
        return handle.model_copy(update={"state": TaskState.CANCELED, "inline_result": None})

    # -- internals -------------------------------------------------------- #

    def _task(self, handle: VideoTaskHandle) -> _EchoTask:
        task = self._tasks.get(handle.task_id)
        if task is None:
            raise VideoProviderError(
                f"unknown echo task {handle.task_id}",
                provider_id=self.provider_id,
                task_id=handle.task_id,
            )
        return task

    def _validate_against_capability(self, request: CanonicalVideoRequest) -> None:
        cap = self._capability
        if not cap.supports_mode(request.mode):
            raise VideoProviderError(
                f"{self.provider_id} does not support mode {request.mode.value}",
                provider_id=self.provider_id,
            )
        if not cap.allows_duration(request.duration_s):
            raise VideoProviderError(
                f"{self.provider_id} cannot render {request.duration_s}s "
                f"(window [{cap.min_duration_s}, {cap.max_duration_s}]s)",
                provider_id=self.provider_id,
            )

    def _build_result(
        self, task_id: str, request: CanonicalVideoRequest
    ) -> CanonicalVideoResult:
        """A byte-deterministic fake clip + last frame derived from the request."""
        key = request.idempotency_key()
        clip = b"ECHO-MP4-" + hashlib.sha256(key.encode()).digest()
        last_frame = b"ECHO-PNG-" + hashlib.sha256(b"frame:" + key.encode()).digest()
        cap = self._capability
        return CanonicalVideoResult(
            provider_id=self.provider_id,
            mode=request.mode,
            model=request.model or f"{self.provider_id}-model",
            duration_s=cap.snap_duration(request.duration_s),
            clip_url=f"echo://{task_id}.mp4",
            clip_bytes=clip,
            last_frame_bytes=last_frame,
            resolution=request.resolution or cap.default_resolution,
            fps=request.fps or cap.default_fps,
            provider_task_id=task_id,
            seed=request.seed,
        )


__all__ = [
    "EchoVideoProvider",
    "default_echo_capability",
]
