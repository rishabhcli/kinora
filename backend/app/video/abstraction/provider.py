"""The :class:`UniversalVideoProvider` contract every video adapter implements.

This is THE seam the rest of the video subsystem plugs into. A provider declares
what it can do (:meth:`capabilities`) and exposes the four-call async lifecycle
that covers both sync and async model APIs:

    submit → poll → fetch        (+ cancel)

* **submit** turns a :class:`~app.video.abstraction.schema.CanonicalVideoRequest`
  into a :class:`~app.video.abstraction.schema.VideoTaskHandle`. A synchronous
  provider does the whole render here and returns a terminal handle with an
  ``inline_result`` attached.
* **poll** refreshes the handle's :class:`~app.video.abstraction.schema.TaskState`
  without downloading the clip (the §9.7 Rendering→QA transition watches this).
* **fetch** returns the finished
  :class:`~app.video.abstraction.schema.CanonicalVideoResult` once terminal-OK.
* **cancel** best-effort aborts an in-flight task (some regions can't; the
  capability declares it).

The Generator/Scheduler never branches on provider identity — it asks the
registry for a provider matching a :class:`CapabilityQuery` (§9.2/§9.3) and drives
this contract. Two equivalent forms are offered: a structural
:class:`UniversalVideoProvider` ``Protocol`` (duck-typed; an existing object can
satisfy it without inheritance) and a :class:`BaseVideoProvider` ABC with a
``render`` convenience that runs the submit→poll→fetch loop for you.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from typing import Protocol, runtime_checkable

from app.core.logging import get_logger

from .capability import VideoCapability
from .schema import (
    CanonicalVideoRequest,
    CanonicalVideoResult,
    TaskState,
    VideoTaskHandle,
)

logger = get_logger("app.video.abstraction.provider")


class VideoProviderError(Exception):
    """A provider-layer failure raised by a :class:`UniversalVideoProvider`.

    Deliberately mirrors :class:`app.providers.errors.ProviderError`'s
    ``retryable`` flag so the existing router / pipeline can treat an abstraction
    provider's failures identically (retryable transport fault vs. hard bad
    request). Adapters wrapping a concrete provider should preserve the underlying
    error's retryability.
    """

    def __init__(
        self,
        message: str,
        *,
        provider_id: str | None = None,
        retryable: bool = False,
        task_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.provider_id = provider_id
        self.retryable = retryable
        self.task_id = task_id


class VideoRenderTimeout(VideoProviderError):  # noqa: N818 — public name in contract
    """The submit→poll loop exceeded its deadline before reaching a terminal state."""

    def __init__(self, message: str, **kwargs: object) -> None:
        kwargs.setdefault("retryable", True)
        super().__init__(message, **kwargs)  # type: ignore[arg-type]


@runtime_checkable
class UniversalVideoProvider(Protocol):
    """Structural contract for any video-gen adapter (duck-typed).

    Implementations need not inherit from anything — matching these five members
    is enough for ``isinstance(obj, UniversalVideoProvider)`` (runtime-checkable)
    and for registration in the
    :class:`~app.video.abstraction.registry.ProviderRegistry`.
    """

    #: Stable provider id; MUST equal ``capabilities().provider_id``.
    provider_id: str

    def capabilities(self) -> VideoCapability:
        """The provider's declared envelope (pure; no I/O)."""
        ...

    async def submit(self, request: CanonicalVideoRequest) -> VideoTaskHandle:
        """Begin a render; return a handle to poll/fetch/cancel."""
        ...

    async def poll(self, handle: VideoTaskHandle) -> VideoTaskHandle:
        """Refresh and return ``handle`` with the latest task state (no download)."""
        ...

    async def fetch(self, handle: VideoTaskHandle) -> CanonicalVideoResult:
        """Return the finished result for a terminal-OK ``handle``."""
        ...

    async def cancel(self, handle: VideoTaskHandle) -> VideoTaskHandle:
        """Best-effort cancel an in-flight task; return the updated handle."""
        ...


class BaseVideoProvider(ABC):
    """An ABC base for adapters that adds a ``render`` submit→poll→fetch loop.

    Subclasses implement the same five members as the protocol; in return they
    get :meth:`render`, a deterministic, injectable-sleep polling loop that drives
    a task from submit to a terminal state and fetches the result. The loop:

    * returns an attached ``inline_result`` immediately (synchronous providers);
    * polls with linear→capped backoff until terminal;
    * raises :class:`VideoRenderTimeout` if the deadline elapses first;
    * raises :class:`VideoProviderError` on a FAILED / CANCELED terminal state.

    The sleep + clock are injectable so tests advance time with zero real waiting.
    """

    provider_id: str

    @abstractmethod
    def capabilities(self) -> VideoCapability: ...

    @abstractmethod
    async def submit(self, request: CanonicalVideoRequest) -> VideoTaskHandle: ...

    @abstractmethod
    async def poll(self, handle: VideoTaskHandle) -> VideoTaskHandle: ...

    @abstractmethod
    async def fetch(self, handle: VideoTaskHandle) -> CanonicalVideoResult: ...

    @abstractmethod
    async def cancel(self, handle: VideoTaskHandle) -> VideoTaskHandle: ...

    async def render(
        self,
        request: CanonicalVideoRequest,
        *,
        timeout_s: float = 600.0,
        interval_s: float = 3.0,
        max_interval_s: float = 15.0,
        backoff: float = 1.5,
    ) -> CanonicalVideoResult:
        """Run the full submit→poll→fetch lifecycle and return the clip.

        Args:
            request: the canonical render request.
            timeout_s: wall-budget for the whole loop before
                :class:`VideoRenderTimeout`.
            interval_s: initial poll interval.
            max_interval_s: ceiling for the (growing) poll interval.
            backoff: multiplier applied to the interval after each poll.

        Raises:
            VideoProviderError: terminal FAILED/CANCELED, or a provider fault.
            VideoRenderTimeout: deadline elapsed before a terminal state.
        """
        handle = await self.submit(request)
        if handle.state is TaskState.SUCCEEDED and handle.inline_result is not None:
            return handle.inline_result

        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout_s
        interval = interval_s
        while True:
            if handle.state.is_terminal:
                break
            if loop.time() >= deadline:
                raise VideoRenderTimeout(
                    f"render of {request.shot_id or request.idempotency_key()[:12]} "
                    f"did not reach a terminal state within {timeout_s}s",
                    provider_id=self.provider_id,
                    task_id=handle.task_id,
                )
            await asyncio.sleep(interval)
            interval = min(max_interval_s, interval * backoff)
            handle = await self.poll(handle)

        if handle.state is TaskState.SUCCEEDED:
            if handle.inline_result is not None:
                return handle.inline_result
            return await self.fetch(handle)
        raise VideoProviderError(
            f"render task {handle.task_id} ended {handle.state.value}",
            provider_id=self.provider_id,
            task_id=handle.task_id,
            retryable=handle.state is not TaskState.CANCELED,
        )


__all__ = [
    "BaseVideoProvider",
    "UniversalVideoProvider",
    "VideoProviderError",
    "VideoRenderTimeout",
]
