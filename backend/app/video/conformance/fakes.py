"""Reference + deliberately-broken fake video adapters for the conformance suite.

The harness is only trustworthy if we can prove it (a) passes a *correct*
adapter and (b) catches *each* specific violation. So this module ships:

* :class:`ReferenceVideoProvider` — a faithful, fully-conformant fake driven by
  the :class:`~app.video.conformance.transport.ScriptedTransport`. It declares an
  honest capability profile, maps the canonical :class:`WanSpec` into a native
  body, translates transport faults into the shared taxonomy, eagerly downloads
  clip bytes, extracts a last frame, dedupes submits by ``shot_id``, cancels, and
  honours the spend gate. :func:`run_conformance` on it returns ``passed=True``.
* A family of broken fakes, each violating exactly **one** guarantee, so a test
  can assert the matching :class:`ConformanceCheck` flips to FAIL while the rest
  still pass. They are built by toggling :class:`BrokenBehaviour` flags on the
  reference fake, so the *only* difference from the reference is the defect.

Every fake is constructed via a ``build(script)`` factory (the ``rebuild`` the
runner needs), so each check runs against a fresh provider bound to its own
scripted transport. None of this touches the network or spends video seconds.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from app.providers.errors import (
    LiveVideoDisabled,
    ProviderBadRequest,
    ProviderError,
    ProviderTimeout,
    RateLimited,
)
from app.providers.types import VideoResult, WanMode, WanSpec

from .protocol import (
    ConformantVideoProvider,
    DurationBounds,
    SubmittedTask,
    TaskStatus,
    VideoCapabilities,
)
from .runner import ProviderFactory
from .transport import ScriptedTransport, TransportScript

# --------------------------------------------------------------------------- #
# Defects (each broken fake flips exactly one)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class BrokenBehaviour:
    """Toggles that introduce exactly one conformance defect on the reference.

    Each field maps to one :class:`~app.video.conformance.report.ConformanceCheck`
    the harness must catch. The reference fake uses the all-``False`` default;
    every broken fake flips one flag.
    """

    #: Render an UNDECLARED mode instead of rejecting it (capability over-claim).
    render_undeclared_modes: bool = False
    #: Accept any duration regardless of the declared window.
    accept_any_duration: bool = False
    #: Drop the prompt when building the native body (request-mapping loss).
    drop_prompt_in_body: bool = False
    #: Surface a 429 as a generic error instead of RateLimited (taxonomy break).
    miscategorize_rate_limit: bool = False
    #: Return the expiring URL but never download bytes (asset-handling break).
    skip_download: bool = False
    #: Claim last-frame extraction but return none.
    drop_last_frame: bool = False
    #: Mint a fresh task on every submit even with a repeated shot_id (double-spend).
    break_idempotency: bool = False
    #: cancel() is a no-op that never reaches the transport.
    ignore_cancel: bool = False
    #: A never-completing task raises a generic error instead of ProviderTimeout.
    wrong_timeout_error: bool = False
    #: Submit a task even though the spend gate is closed (spend leak).
    leak_on_closed_gate: bool = False
    #: Declare a capability profile that is internally inconsistent.
    inconsistent_declaration: bool = False


# --------------------------------------------------------------------------- #
# The reference (conformant) fake
# --------------------------------------------------------------------------- #


@dataclass
class _FakeConfig:
    """Construction knobs for a fake provider."""

    name: str = "fake-reference"
    capabilities: VideoCapabilities | None = None
    behaviour: BrokenBehaviour = field(default_factory=BrokenBehaviour)
    live: bool = True  # spend gate OPEN by default so the happy path renders
    poll_budget: int = 64  # max polls before a one-shot render times out


_DEFAULT_CAPS = VideoCapabilities(
    provider_id="fake-reference",
    modes=frozenset(
        {
            WanMode.TEXT_TO_VIDEO,
            WanMode.IMAGE_TO_VIDEO,
            WanMode.FIRST_LAST_FRAME,
            WanMode.VIDEO_CONTINUATION,
        }
    ),
    durations=DurationBounds(min_s=5, max_s=15),
    resolutions=frozenset({"480P", "720P", "1080P"}),
    supports_seed=True,
    supports_negative_prompt=True,
    eager_download=True,
    extracts_last_frame=True,
    staged_lifecycle=True,
    cancellable=True,
    idempotent_submit=True,
)


class ReferenceVideoProvider:
    """A fully-conformant fake adapter over a :class:`ScriptedTransport`.

    Satisfies :class:`~app.video.conformance.protocol.ConformantVideoProvider`
    end-to-end: honest capabilities, faithful request mapping, taxonomy-correct
    error translation, eager download + last-frame extraction, idempotent
    submit, cancellation, timeout, and spend-gate honouring. The
    :class:`BrokenBehaviour` flags let derived fakes inject a single defect; with
    the default (all-``False``) behaviour, ``run_conformance`` returns
    ``passed=True``.
    """

    def __init__(self, transport: ScriptedTransport, config: _FakeConfig | None = None) -> None:
        self.transport = transport
        self._cfg = config or _FakeConfig()
        caps = self._cfg.capabilities or _DEFAULT_CAPS
        self._caps = replace(caps, provider_id=self._cfg.name)
        self.name = self._cfg.name
        self._b = self._cfg.behaviour

    # -- contract: capabilities ------------------------------------------- #

    def capabilities(self) -> VideoCapabilities:
        if self._b.inconsistent_declaration:
            # Claim cancellable + idempotent but NO staged lifecycle (impossible).
            return replace(
                self._caps,
                staged_lifecycle=False,
                cancellable=True,
                idempotent_submit=True,
            )
        return self._caps

    # -- validation (capability guard) ------------------------------------ #

    def _validate(self, spec: WanSpec) -> None:
        caps = self._caps
        if not self._b.render_undeclared_modes and not caps.supports_mode(spec.mode):
            raise ProviderBadRequest(f"mode {spec.mode.value} not supported by {self.name}")
        if not self._b.accept_any_duration and not caps.durations.contains(spec.duration_s):
            raise ProviderBadRequest(
                f"duration {spec.duration_s}s outside [{caps.durations.min_s},"
                f"{caps.durations.max_s}] for {self.name}"
            )
        if not caps.supports_resolution(spec.resolution):
            raise ProviderBadRequest(f"resolution {spec.resolution} not supported by {self.name}")

    # -- request mapping (canonical -> native body) ----------------------- #

    def _native_body(self, spec: WanSpec) -> dict[str, object]:
        body: dict[str, object] = {
            "model": spec.model or self.name,
            "mode": spec.mode.value,
            "duration": spec.duration_s,
            "resolution": spec.resolution,
        }
        if not self._b.drop_prompt_in_body and spec.prompt:
            body["prompt"] = spec.prompt
        if spec.negative_prompt:
            body["negative_prompt"] = spec.negative_prompt
        if spec.seed is not None:
            body["seed"] = spec.seed
        media: list[dict[str, str]] = []
        for url in (
            spec.image_url,
            spec.first_frame_url,
            spec.last_frame_url,
            spec.source_video_url,
            *spec.reference_image_urls,
        ):
            if url:
                media.append({"url": url})
        if media:
            body["media"] = media
        return body

    # -- staged lifecycle ------------------------------------------------- #

    async def submit(self, spec: WanSpec) -> SubmittedTask:
        if not self._cfg.live:
            raise LiveVideoDisabled("KINORA_LIVE_VIDEO is off; no task submitted")
        self._validate(spec)
        body = self._native_body(spec)
        shot_id = None if self._b.break_idempotency else spec.shot_id
        try:
            task_id = self.transport.submit(body, shot_id=shot_id)
        except RateLimited as exc:
            if self._b.miscategorize_rate_limit:
                # Defect: collapse a 429 into a generic, non-retryable error
                # instead of preserving the RateLimited taxonomy member.
                raise ProviderError(f"rate limited (miscategorized): {exc.message}") from exc
            raise
        return SubmittedTask(task_id=task_id, model=str(body["model"]), shot_id=spec.shot_id)

    async def poll(self, task: SubmittedTask) -> TaskStatus:
        state = self.transport.poll(task.task_id)
        clip_url = self.transport.clip_url(task.task_id) if state == "succeeded" else None
        return TaskStatus(task_id=task.task_id, state=state, clip_url=clip_url)

    async def fetch(self, task: SubmittedTask) -> VideoResult:
        clip_bytes: bytes | None = None
        if not self._b.skip_download:
            url = self.transport.clip_url(task.task_id)
            clip_bytes = self.transport.download(url)
        last_frame = None
        if self._caps.extracts_last_frame and not self._b.drop_last_frame:
            last_frame = self.transport.last_frame(task.task_id)
        # Duration/mode are carried on the SubmittedTask via the body the harness
        # observes; reconstruct from the last submit body for the echo assertions.
        body = self.transport.last_submit_body or {}
        duration = float(body.get("duration", 5))  # type: ignore[arg-type]
        mode = WanMode(str(body.get("mode", WanMode.TEXT_TO_VIDEO.value)))
        return VideoResult(
            duration_s=duration,
            model=task.model,
            mode=mode,
            provider_task_id=task.task_id,
            clip_url=self.transport.clip_url(task.task_id),
            clip_bytes=clip_bytes,
            last_frame_bytes=last_frame,
        )

    async def cancel(self, task: SubmittedTask) -> None:
        if self._b.ignore_cancel:
            return  # defect: never reaches the transport
        self.transport.cancel(task.task_id)

    # -- one-shot render (submit -> poll -> fetch) ------------------------ #

    async def render(self, spec: WanSpec) -> VideoResult:
        if not self._cfg.live:
            if self._b.leak_on_closed_gate:
                # Defect: submit anyway (spend leak) before raising.
                self.transport.submit(self._native_body(spec), shot_id=spec.shot_id)
            raise LiveVideoDisabled("KINORA_LIVE_VIDEO is off; no Wan task submitted")
        task = await self.submit(spec)
        for _ in range(self._cfg.poll_budget):
            status = await self.poll(task)
            if status.state == "succeeded":
                return await self.fetch(task)
            if status.state in ("failed", "canceled"):
                raise ProviderError(f"task {task.task_id} ended {status.state}")
        # Never completed within the poll budget → the canonical timeout.
        if self._b.wrong_timeout_error:
            raise ProviderError("task stalled (wrong error type — should be ProviderTimeout)")
        raise ProviderTimeout(f"task {task.task_id} did not complete within the poll budget")


# --------------------------------------------------------------------------- #
# Factories — each returns a ``rebuild(script) -> provider`` for the runner
# --------------------------------------------------------------------------- #


def reference_factory(
    *,
    name: str = "fake-reference",
    live: bool = True,
    behaviour: BrokenBehaviour | None = None,
    capabilities: VideoCapabilities | None = None,
) -> ProviderFactory:
    """Build a ``rebuild(script)`` factory for a (possibly broken) reference fake."""

    def build(script: TransportScript) -> ReferenceVideoProvider:
        return ReferenceVideoProvider(
            ScriptedTransport(script),
            _FakeConfig(
                name=name,
                live=live,
                behaviour=behaviour or BrokenBehaviour(),
                capabilities=capabilities,
            ),
        )

    return build


def make_reference(*, live: bool = True) -> ReferenceVideoProvider:
    """A single reference provider instance (the runner can rebuild via the factory)."""
    return ReferenceVideoProvider(ScriptedTransport(), _FakeConfig(live=live))


@dataclass(frozen=True, slots=True)
class FakeKit:
    """A provider plus the open- and closed-gate factories ``run_conformance`` wants.

    Bundles everything a conformance run needs for a (reference or broken) fake:
    a seed ``provider`` instance, the gate-open ``rebuild`` factory, and the
    gate-closed ``rebuild_gated`` factory — so the SPEND_GATE check can verify
    the deliberate gate path. Pass these straight through to
    :func:`~app.video.conformance.runner.run_conformance`.
    """

    provider: ConformantVideoProvider
    rebuild: ProviderFactory
    rebuild_gated: ProviderFactory


def fake_kit(
    *,
    name: str = "fake-reference",
    behaviour: BrokenBehaviour | None = None,
    capabilities: VideoCapabilities | None = None,
) -> FakeKit:
    """Assemble a :class:`FakeKit` for the reference fake or a broken variant."""
    behaviour = behaviour or BrokenBehaviour()
    rebuild = reference_factory(
        name=name, live=True, behaviour=behaviour, capabilities=capabilities
    )
    rebuild_gated = reference_factory(
        name=name, live=False, behaviour=behaviour, capabilities=capabilities
    )
    provider = rebuild(TransportScript.healthy())
    return FakeKit(provider=provider, rebuild=rebuild, rebuild_gated=rebuild_gated)


#: A registry of named broken fakes, each violating exactly one check, mapping a
#: provider id to the ``BrokenBehaviour`` that breaks it. Tests iterate this to
#: prove the harness catches every violation.
BROKEN_BEHAVIOURS: dict[str, BrokenBehaviour] = {
    "broken-undeclared-mode": BrokenBehaviour(render_undeclared_modes=True),
    "broken-any-duration": BrokenBehaviour(accept_any_duration=True),
    "broken-drop-prompt": BrokenBehaviour(drop_prompt_in_body=True),
    "broken-taxonomy": BrokenBehaviour(miscategorize_rate_limit=True),
    "broken-no-download": BrokenBehaviour(skip_download=True),
    "broken-no-last-frame": BrokenBehaviour(drop_last_frame=True),
    "broken-double-spend": BrokenBehaviour(break_idempotency=True),
    "broken-ignore-cancel": BrokenBehaviour(ignore_cancel=True),
    "broken-wrong-timeout": BrokenBehaviour(wrong_timeout_error=True),
    "broken-spend-leak": BrokenBehaviour(leak_on_closed_gate=True),
    "broken-declaration": BrokenBehaviour(inconsistent_declaration=True),
}


__all__ = [
    "BROKEN_BEHAVIOURS",
    "BrokenBehaviour",
    "FakeKit",
    "ReferenceVideoProvider",
    "fake_kit",
    "make_reference",
    "reference_factory",
]
