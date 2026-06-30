"""``GeneratorBridge`` — a drop-in ``VideoBackend`` backed by the facade.

The render pipeline injects a :class:`~app.providers.video_router.VideoBackend`
into the :class:`~app.agents.generator.Generator` (``video_backend=...``). That
seam is exactly one async method: ``render(WanSpec) -> VideoResult``, with the
contract that a closed spend gate raises ``LiveVideoDisabled`` (the pipeline then
degrades to the ffmpeg Ken-Burns lane).

:class:`GeneratorBridge` *is* such a backend, but it routes every render through
the unified :class:`VideoGenerationService` — so the Generator transparently
gains capability planning, multi-provider failover, identity conditioning,
prompt-dialect compilation, the async job lifecycle, cost reservation, the
quality gate, and output normalization, **without any change to
``app/agents/generator.py``**. You wire it by passing
``Generator(providers, video_backend=GeneratorBridge(service))``.

A ``WanSpec`` carries no ``ShotSpec`` (the Generator already lowered the shot to a
spec before calling the backend), so the bridge reconstructs a minimal
:class:`~app.agents.contracts.ShotSpec` from the spec for the facade's
identity/dialect steps. When the facade is wired with the neutral adapters (the
default), that reconstructed shot is a faithful round-trip; when it's wired with
the real identity conditioner you instead use :meth:`render_shot` directly with
the original shot for full fidelity.
"""

from __future__ import annotations

from app.agents.contracts import Camera, RenderMode, ShotSpec
from app.providers.errors import LiveVideoDisabled, ProviderError, ProviderTimeout
from app.providers.types import VideoResult, WanMode, WanSpec

from .models import SkipReason, VideoGenerationRequest, VideoGenerationResult
from .service import VideoGenerationService

#: WanMode → RenderMode (the inverse of the Generator's 1:1 value map).
_WAN_TO_RENDER: dict[str, RenderMode] = {mode.value: RenderMode(mode.value) for mode in WanMode}


class GeneratorBridge:
    """A :class:`VideoBackend` whose ``render`` delegates to the facade.

    Satisfies the structural ``VideoBackend`` protocol (``name`` / ``render`` /
    ``healthy``) so it is a drop-in for the Generator's ``video_backend`` seam and
    can even nest inside a :class:`~app.providers.video_router.VideoRouter`.
    """

    def __init__(
        self,
        service: VideoGenerationService,
        *,
        book_id: str = "",
        session_id: str | None = None,
        name: str = "video-service-bridge",
    ) -> None:
        self._service = service
        self._book_id = book_id
        self._session_id = session_id
        self.name = name

    async def healthy(self) -> bool:
        """Always routable: the facade owns its own provider health internally."""
        return True

    async def render(self, spec: WanSpec) -> VideoResult:
        """Render ``spec`` via the facade, re-raising a closed gate / fault.

        The Generator/pipeline seam expects ``LiveVideoDisabled`` (or a
        ``ProviderError``) on a non-render so it can degrade. A facade ``SKIPPED``
        result is therefore translated back into the matching exception; a
        ``GENERATED`` result is unwrapped into a provider :class:`VideoResult`.
        """
        request = self._request_for(spec)
        result = await self._service.generate(request)
        return self._to_video_result(result, spec)

    async def render_shot(
        self,
        shot: ShotSpec,
        *,
        book_id: str | None = None,
        session_id: str | None = None,
        target_duration_s: float | None = None,
    ) -> VideoGenerationResult:
        """Full-fidelity entry: hand the *original* shot to the facade directly.

        Prefer this over :meth:`render` when the caller still holds the
        :class:`ShotSpec` — the facade's identity/dialect steps then see the real
        shot (locked refs, camera block) instead of a spec round-trip. Returns the
        rich :class:`VideoGenerationResult` (accept-or-degrade), letting the caller
        branch on the outcome rather than catching an exception.
        """
        request = VideoGenerationRequest(
            shot=shot,
            book_id=book_id if book_id is not None else self._book_id,
            session_id=session_id if session_id is not None else self._session_id,
            target_duration_s=target_duration_s,
        )
        return await self._service.generate(request)

    # -- spec <-> shot round-trip ------------------------------------------- #

    def _request_for(self, spec: WanSpec) -> VideoGenerationRequest:
        shot = self._shot_for(spec)
        return VideoGenerationRequest(
            shot=shot,
            book_id=self._book_id,
            session_id=self._session_id,
            target_duration_s=float(spec.duration_s),
        )

    @staticmethod
    def _shot_for(spec: WanSpec) -> ShotSpec:
        """Reconstruct a minimal :class:`ShotSpec` from a :class:`WanSpec`."""
        return ShotSpec(
            shot_id=spec.shot_id or "bridge-shot",
            render_mode=_WAN_TO_RENDER[spec.mode.value],
            prompt=spec.prompt,
            negative_prompt=spec.negative_prompt,
            camera=Camera(),
            seed=spec.seed if spec.seed is not None else 0,
            target_duration_s=float(spec.duration_s),
        )

    @staticmethod
    def _to_video_result(result: VideoGenerationResult, spec: WanSpec) -> VideoResult:
        if result.generated and result.clip is not None:
            clip = result.clip
            return VideoResult(
                duration_s=clip.duration_s or float(spec.duration_s),
                model=result.model or (spec.model or "video"),
                mode=spec.mode,
                provider_task_id=result.provider_task_id,
                clip_url=clip.clip_url,
                clip_bytes=clip.clip_bytes,
                last_frame_bytes=clip.last_frame_bytes,
            )
        # A SKIP is surfaced as the exception the existing seam degrades on.
        reason = result.skip_reason
        if reason is SkipReason.LIVE_DISABLED:
            raise LiveVideoDisabled(
                "video generation skipped: live video disabled (no clip rendered)"
            )
        if reason is SkipReason.JOB_TIMEOUT:
            raise ProviderTimeout("video generation timed out (no clip rendered)")
        # BUDGET_EXCEEDED / PLANNER_SKIP / PROVIDER_FAILED / QUALITY_REJECTED all
        # degrade the same way at the pipeline; a retryable ProviderError lets the
        # pipeline's own degrade path take over without sinking the whole render.
        raise ProviderError(
            f"video generation skipped ({reason.value if reason else 'unknown'}); "
            "no clip rendered",
            retryable=False,
        )


__all__ = ["GeneratorBridge"]
