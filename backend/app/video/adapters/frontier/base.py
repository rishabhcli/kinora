"""The shared base adapter: lifecycle, gating, capability validation, usage.

Concrete frontier adapters (Runway / Luma / Pika / Kling / Veo / Sora) subclass
:class:`BaseFrontierAdapter` and implement only the *provider-specific* seams:

* :meth:`_capabilities` — the declared :class:`CapabilityProfile`.
* :meth:`_build_submit` — canonical request → (path, native JSON body).
* :meth:`_parse_submit` — submit response → provider job id.
* :meth:`_build_poll` — job id → (method, path, params).
* :meth:`_parse_poll` — poll response → :class:`PollResult`.

Everything else — the two spend gates, capability pre-flight, duration snapping,
the submit→poll→fetch composition, eager download, usage accounting, and the
:class:`~app.providers.video_router.VideoBackend` surface — lives here, written once.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from typing import Any

from app.core.config import Settings
from app.core.logging import get_logger
from app.providers.types import Usage, VideoResult, WanSpec

from .errors import FrontierError, FrontierJobCanceled, FrontierJobFailed, FrontierTimeout
from .transport import FrontierTransport
from .types import (
    CapabilityProfile,
    FetchedClip,
    FrontierRequest,
    JobStatus,
    PollResult,
    SubmitHandle,
    from_wan_spec,
    mode_to_wan,
    validate_against_profile,
)

logger = get_logger("app.video.adapters.frontier")

#: Optional usage sink — receives a Usage per completed render (defaults to a no-op).
UsageRecorder = Any  # Callable[[Usage], None]


class BaseFrontierAdapter(ABC):
    """Common machinery for every frontier hosted video adapter.

    The render lifecycle is async-job: ``submit`` → poll-loop → ``fetch``. ``render``
    composes them and returns a :class:`~app.providers.types.VideoResult`, so the
    adapter is a drop-in :class:`~app.providers.video_router.VideoBackend`.
    """

    #: Subclasses set a stable provider slug (e.g. "runway").
    provider_slug: str = "frontier"

    def __init__(
        self,
        settings: Settings,
        transport: FrontierTransport,
        *,
        model: str | None = None,
        usage_recorder: UsageRecorder | None = None,
        poll_timeout_s: float | None = None,
        poll_interval_s: float | None = None,
        poll_max_interval_s: float | None = None,
        name: str | None = None,
    ) -> None:
        self._settings = settings
        self._transport = transport
        self._model = model or self._default_model(settings)
        self._usage_recorder = usage_recorder
        self._poll_timeout_s = (
            poll_timeout_s if poll_timeout_s is not None else settings.frontier_poll_timeout_s
        )
        self._poll_interval_s = (
            poll_interval_s if poll_interval_s is not None else settings.frontier_poll_interval_s
        )
        self._poll_max_interval_s = (
            poll_max_interval_s
            if poll_max_interval_s is not None
            else settings.frontier_poll_max_interval_s
        )
        self.name = name or f"{self.provider_slug}:{self._model}"

    # -- provider-specific seams (subclasses implement) ------------------- #

    @abstractmethod
    def _default_model(self, settings: Settings) -> str:
        """The provider's configured default model id."""

    @abstractmethod
    def _capabilities(self) -> CapabilityProfile:
        """The declared capability envelope (subclasses build this)."""

    @abstractmethod
    def _build_submit(self, request: FrontierRequest) -> tuple[str, dict[str, Any]]:
        """Map the canonical request → (submit path, native JSON body)."""

    @abstractmethod
    def _parse_submit(self, body: dict[str, Any]) -> str:
        """Extract the provider job id from the submit response."""

    @abstractmethod
    def _build_poll(self, handle: SubmitHandle) -> tuple[str, str, dict[str, Any] | None]:
        """Map a handle → (HTTP method, poll path, query params|None)."""

    @abstractmethod
    def _parse_poll(self, body: dict[str, Any]) -> PollResult:
        """Map the poll response → a canonical :class:`PollResult`."""

    # -- public capability surface ---------------------------------------- #

    def capabilities(self) -> CapabilityProfile:
        return self._capabilities()

    async def healthy(self) -> bool:
        """Cheap liveness probe — no network, no spend.

        Returns ``True`` whenever the adapter *could* route (the gate, not the
        backend, is what blocks a render). With the global gate off, every render
        raises :class:`~app.providers.errors.LiveVideoDisabled`, surfaced at
        ``render`` — never here.
        """
        return True

    # -- the async-job lifecycle ------------------------------------------ #

    def prepare(self, request: FrontierRequest) -> FrontierRequest:
        """Snap the request to the provider's capabilities + validate it (pure-ish).

        Duration is *snapped* to the nearest supported discrete value; resolution and
        aspect ratio default to the profile's first option when the caller left the
        canonical defaults that this provider doesn't list. Then the snapped request
        is validated against the profile, raising
        :class:`~app.video.adapters.frontier.errors.FrontierUnsupportedCapability`.
        """
        profile = self._capabilities()
        snapped = request.model_copy(
            update={
                "duration_s": profile.nearest_duration(request.duration_s),
                "resolution": self._snap_resolution(request, profile),
                "aspect_ratio": self._snap_aspect(request, profile),
            }
        )
        validate_against_profile(snapped, profile)
        return snapped

    @staticmethod
    def _snap_resolution(request: FrontierRequest, profile: CapabilityProfile) -> str:
        if request.resolution in profile.resolutions or not profile.resolutions:
            return request.resolution
        # The canonical default ("720p") is a *request hint*; if the provider doesn't
        # offer it, fall back to the provider's first (preferred) resolution rather
        # than rejecting. A non-default explicit choice still validates → may raise.
        if request.resolution == "720p":
            return profile.default_resolution()
        return request.resolution

    @staticmethod
    def _snap_aspect(request: FrontierRequest, profile: CapabilityProfile) -> str:
        if request.aspect_ratio in profile.aspect_ratios or not profile.aspect_ratios:
            return request.aspect_ratio
        if request.aspect_ratio == "16:9":
            return profile.default_aspect_ratio()
        return request.aspect_ratio

    async def submit(self, request: FrontierRequest) -> SubmitHandle:
        """Gate, validate, and submit the request; return a poll handle.

        Order of guards (no spend until the very last step):
        1. ``LiveVideoDisabled`` when the global ``KINORA_LIVE_VIDEO`` gate is off.
        2. ``FrontierUnsupportedCapability`` (capability pre-flight, local).
        3. ``FrontierTransportDisabled`` (transport flag) — enforced inside the
           transport on the actual HTTP call.
        """
        self._check_live_gate()
        prepared = self.prepare(request)
        path, body = self._build_submit(prepared)
        resp = await self._transport.request_json(
            "POST", path, op=f"{self.provider_slug}_submit", json=body
        )
        job_id = self._parse_submit(resp)
        if not job_id:
            raise FrontierError(
                f"{self.provider_slug} submission returned no job id",
                provider=self.provider_slug,
            )
        logger.info(
            "frontier.submitted",
            provider=self.provider_slug,
            model=self._model,
            job_id=job_id,
            mode=prepared.mode.value,
        )
        return SubmitHandle(provider=self.provider_slug, model=self._model, job_id=job_id)

    async def poll(self, handle: SubmitHandle) -> PollResult:
        """Poll a submitted job once for its current status."""
        method, path, params = self._build_poll(handle)
        resp = await self._transport.request_json(
            method, path, op=f"{self.provider_slug}_poll", params=params
        )
        return self._parse_poll(resp)

    async def poll_to_completion(self, handle: SubmitHandle) -> PollResult:
        """Poll with bounded backoff until terminal or the deadline elapses."""
        deadline = time.monotonic() + self._poll_timeout_s
        interval = self._poll_interval_s
        while True:
            result = await self.poll(handle)
            if result.status is JobStatus.SUCCEEDED:
                return result
            if result.status is JobStatus.FAILED:
                detail = result.detail or ""
                raise FrontierJobFailed(
                    f"{self.provider_slug} job {handle.job_id} failed: {detail}".strip(),
                    provider=self.provider_slug,
                    request_id=handle.job_id,
                )
            if result.status is JobStatus.CANCELED:
                raise FrontierJobCanceled(
                    f"{self.provider_slug} job {handle.job_id} was canceled",
                    provider=self.provider_slug,
                    request_id=handle.job_id,
                )
            if time.monotonic() >= deadline:
                raise FrontierTimeout(
                    f"{self.provider_slug} job {handle.job_id} did not complete within "
                    f"{self._poll_timeout_s}s",
                    provider=self.provider_slug,
                    request_id=handle.job_id,
                )
            await self._transport._sleep(
                interval
            )  # noqa: SLF001 - shared sleeper (test-injectable)
            interval = min(self._poll_max_interval_s, interval * 1.5)

    async def fetch(self, poll_result: PollResult) -> FetchedClip:
        """Download the produced asset bytes eagerly (provider URLs expire)."""
        if poll_result.status is not JobStatus.SUCCEEDED or not poll_result.asset_url:
            raise FrontierError(
                f"{self.provider_slug} fetch called without a succeeded asset URL",
                provider=self.provider_slug,
            )
        clip_bytes = await self._transport.download(
            poll_result.asset_url, op=f"{self.provider_slug}_clip"
        )
        last_frame_bytes: bytes | None = None
        if poll_result.last_frame_url:
            last_frame_bytes = await self._transport.download(
                poll_result.last_frame_url, op=f"{self.provider_slug}_lastframe"
            )
        return FetchedClip(
            clip_bytes=clip_bytes,
            last_frame_bytes=last_frame_bytes,
            clip_url=poll_result.asset_url,
            duration_s=poll_result.duration_s or 0.0,
        )

    # -- the VideoBackend surface ----------------------------------------- #

    async def render(self, spec: WanSpec) -> VideoResult:
        """Submit → poll-to-completion → fetch → :class:`VideoResult`. Gated.

        Raises:
            LiveVideoDisabled: when ``KINORA_LIVE_VIDEO`` is off (no network).
            FrontierUnsupportedCapability: when the spec violates this provider.
            FrontierError (+ subclasses): mapped provider faults.
        """
        request = from_wan_spec(spec)
        handle = await self.submit(request)
        poll_result = await self.poll_to_completion(handle)
        fetched = await self.fetch(poll_result)
        duration = fetched.duration_s or self.prepare(request).duration_s
        self._record_usage(handle.job_id, duration)
        return VideoResult(
            duration_s=float(duration),
            model=self._model,
            mode=mode_to_wan(request.mode),
            provider_task_id=handle.job_id,
            clip_url=fetched.clip_url,
            clip_bytes=fetched.clip_bytes,
            last_frame_bytes=fetched.last_frame_bytes,
        )

    # -- internals -------------------------------------------------------- #

    def _check_live_gate(self) -> None:
        if not self._settings.kinora_live_video:
            from app.providers.errors import LiveVideoDisabled

            raise LiveVideoDisabled(
                f"live video rendering is disabled (KINORA_LIVE_VIDEO is off); "
                f"no {self.provider_slug} task submitted",
            )

    def _record_usage(self, job_id: str, duration_s: float) -> None:
        usage = Usage(
            model=self._model,
            operation="video",
            video_seconds=float(duration_s),
            request_id=job_id,
        )
        if self._usage_recorder is not None:
            try:
                self._usage_recorder(usage)
            except Exception:  # noqa: BLE001 - a broken sink must not fail a good render
                logger.warning("frontier.usage_sink_error", provider=self.provider_slug)


__all__ = ["BaseFrontierAdapter", "UsageRecorder"]
