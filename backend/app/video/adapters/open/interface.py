"""The common interface every open / self-hosted / gateway video adapter implements.

This package adapts *open-weights* and *self-hosted* / *gateway* video models
(Stability SVD, Genmo Mochi, CogVideoX, Lightricks LTX-Video, HunyuanVideo) plus a
**meta-adapter family** (Replicate, fal.ai, ComfyUI / OpenAPI) so a brand-new model
can be onboarded by *configuration alone*. Each adapter:

* declares its :class:`Capabilities` (modes, durations, resolutions, conditioning);
* maps a canonical :class:`WanSpec` render request to the model's native payload;
* **submits → polls → fetches**, downloading the clip bytes *eagerly* (provider
  result URLs expire), and extracting the last frame for continuity;
* normalizes provider faults into the shared typed :mod:`app.providers.errors`.

To keep this subsystem mergeable *before* the rest of the video layer lands, the
adapters target a **local** :class:`OpenVideoBackend` Protocol that mirrors
:class:`app.providers.video_router.VideoBackend` (``name`` / ``render`` /
``healthy``) and adds the finer-grained :class:`SubmitPollFetch` lifecycle the
router never needed to see. A :class:`VideoProvider`-style instance therefore drops
straight into a ``VideoRouter`` with no caller change once merged.

Nothing here touches the network unless a default-OFF transport flag is flipped
on; the spend gate (``KINORA_LIVE_VIDEO``) is honoured and surfaced *unchanged*.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

# The canonical request/result/mode types are the *shared* provider contract — we
# never fork them. Adapters translate WanSpec → native and native → VideoResult.
from app.providers.types import VideoResult, WanMode, WanSpec

__all__ = [
    "Capabilities",
    "ConditioningInput",
    "OpenVideoBackend",
    "SubmitPollFetch",
    "SubmittedTask",
    "TaskState",
    "TaskStatus",
    "VideoResult",
    "WanMode",
    "WanSpec",
]


# --------------------------------------------------------------------------- #
# Capability declaration
# --------------------------------------------------------------------------- #


class ConditioningInput(Protocol):  # pragma: no cover - structural marker only
    """Marker for the conditioning channels a model can accept."""


@dataclass(frozen=True, slots=True)
class Capabilities:
    """A static, declarative profile of what one open-model adapter can render.

    The planner / router uses this to reject a :class:`WanSpec` *before* spending
    a submission, and to pick a backend whose conditioning matches the shot. It is
    intentionally a plain dataclass (no env reads, no I/O) so it is trivially
    serialisable into a provider descriptor (see :mod:`.descriptor`).

    Attributes:
        name: Stable adapter identity (routing + telemetry).
        modes: The :class:`WanMode` s this backend can satisfy.
        max_duration_s: Longest single clip the model produces (seconds).
        min_duration_s: Shortest meaningful clip.
        resolutions: Accepted resolution tokens (e.g. ``"720P"``, ``"480P"``).
        supports_seed: Whether a deterministic ``seed`` is honoured.
        supports_negative_prompt: Whether a negative prompt is accepted.
        supports_audio: Whether the model emits an audio track.
        max_reference_images: Max r2v reference images (0 ⇒ no r2v).
        default_fps: Frames-per-second the model targets (for duration math).
        cost_per_s: Relative cost of one rendered second (ratios only; for
            cost-aware routing). ``0`` ⇒ unknown.
        quality: 0..1 fidelity score for quality-aware routing.
        self_hosted: True for a local/self-hosted endpoint (no metered spend).
    """

    name: str
    modes: frozenset[WanMode] = field(default_factory=frozenset)
    max_duration_s: float = 5.0
    min_duration_s: float = 1.0
    resolutions: frozenset[str] = field(default_factory=lambda: frozenset({"720P"}))
    supports_seed: bool = True
    supports_negative_prompt: bool = False
    supports_audio: bool = False
    max_reference_images: int = 0
    default_fps: int = 24
    cost_per_s: float = 0.0
    quality: float = 0.5
    self_hosted: bool = False

    def supports(self, spec: WanSpec) -> bool:
        """True when this backend can, in principle, satisfy ``spec``.

        Pure structural check: mode membership, duration window, resolution token,
        and (for r2v) the reference-image count. Does **not** consider health,
        budget, or live-gate state — those are the router's concern.
        """
        if spec.mode not in self.modes:
            return False
        if not (self.min_duration_s <= float(spec.duration_s) <= self.max_duration_s):
            return False
        if self.resolutions and spec.resolution not in self.resolutions:
            return False
        if spec.mode is WanMode.REFERENCE_TO_VIDEO:
            refs = len(spec.reference_image_urls)
            if refs == 0 or refs > self.max_reference_images:
                return False
        return True

    def reasons_unsupported(self, spec: WanSpec) -> list[str]:
        """Human-readable reasons ``spec`` is unsupported (empty ⇒ supported).

        Mirrors :meth:`supports` but enumerates *every* mismatch, so a bad-request
        error can tell the caller exactly what to fix instead of failing opaquely.
        """
        reasons: list[str] = []
        if spec.mode not in self.modes:
            allowed = ", ".join(sorted(m.value for m in self.modes)) or "(none)"
            reasons.append(f"mode {spec.mode.value!r} not in supported modes [{allowed}]")
        dur = float(spec.duration_s)
        if dur > self.max_duration_s:
            reasons.append(f"duration {dur}s exceeds max {self.max_duration_s}s")
        if dur < self.min_duration_s:
            reasons.append(f"duration {dur}s below min {self.min_duration_s}s")
        if self.resolutions and spec.resolution not in self.resolutions:
            allowed = ", ".join(sorted(self.resolutions))
            reasons.append(f"resolution {spec.resolution!r} not in [{allowed}]")
        if spec.mode is WanMode.REFERENCE_TO_VIDEO:
            refs = len(spec.reference_image_urls)
            if refs == 0:
                reasons.append("reference_to_video requires at least one reference image")
            elif refs > self.max_reference_images:
                reasons.append(f"{refs} reference images exceeds max {self.max_reference_images}")
        return reasons


# --------------------------------------------------------------------------- #
# Async task lifecycle
# --------------------------------------------------------------------------- #


class TaskState(str):
    """A normalized lifecycle phase for an async render task."""


@dataclass(frozen=True, slots=True)
class TaskStatus:
    """Normalized status of a polled async render task.

    Adapters map their provider's native status vocabulary onto this small set so
    the shared poll loop is provider-agnostic.

    Attributes:
        state: ``"pending"`` (still working), ``"succeeded"``, or ``"failed"``.
        video_url: A fetch URL when ``succeeded`` (may be ``None`` if the provider
            returns bytes inline — then ``inline_bytes`` carries them).
        inline_bytes: Clip bytes returned directly in the status body, if any.
        message: Provider message (carried into error text on failure).
        progress: 0..1 progress when the provider reports it (else ``None``).
        raw: The untouched provider node, for adapter-specific extraction.
    """

    state: str
    video_url: str | None = None
    inline_bytes: bytes | None = None
    message: str | None = None
    progress: float | None = None
    raw: object | None = None

    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"

    @property
    def is_terminal(self) -> bool:
        return self.state in (self.SUCCEEDED, self.FAILED)

    @property
    def ok(self) -> bool:
        return self.state == self.SUCCEEDED


@dataclass(frozen=True, slots=True)
class SubmittedTask:
    """Handle to an in-flight async render returned by :meth:`SubmitPollFetch.submit`.

    Attributes:
        task_id: Provider task / prediction id for polling and idempotency.
        model: The native model id the task was submitted against.
        poll_url: An optional fully-formed status URL (gateways like Replicate
            return one); when ``None`` the adapter builds it from ``task_id``.
        raw: The untouched submission response node.
    """

    task_id: str
    model: str
    poll_url: str | None = None
    raw: object | None = None


@runtime_checkable
class SubmitPollFetch(Protocol):
    """The fine-grained async lifecycle a :class:`BaseOpenAdapter` implements.

    Splitting ``render`` into ``submit`` / ``poll`` / ``fetch`` lets the shared
    base class own the poll loop, the eager download, and last-frame extraction,
    while each concrete adapter supplies only the provider-specific request and
    response mapping. The composite :meth:`render` (on :class:`OpenVideoBackend`)
    drives this lifecycle end-to-end.
    """

    def capabilities(self) -> Capabilities:
        """The static capability profile for this backend."""
        ...

    async def submit(self, spec: WanSpec) -> SubmittedTask:
        """Submit a render; return a poll handle. Raises ``LiveVideoDisabled`` when gated."""
        ...

    async def poll(self, task: SubmittedTask) -> TaskStatus:
        """Poll one status tick for ``task`` and normalize it."""
        ...

    async def fetch(self, task: SubmittedTask, status: TaskStatus) -> bytes:
        """Download the finished clip bytes for a succeeded ``task`` (eager)."""
        ...


@runtime_checkable
class OpenVideoBackend(Protocol):
    """A router-compatible open-model video backend.

    Mirrors :class:`app.providers.video_router.VideoBackend` exactly (``name`` /
    ``render`` / ``healthy``) so an instance is a drop-in router member, and adds
    :meth:`capabilities` for capability-aware planning. Adapters in this package
    satisfy this Protocol structurally.
    """

    name: str

    def capabilities(self) -> Capabilities:
        """The static capability profile (modes/durations/resolutions/...)."""
        ...

    async def render(self, spec: WanSpec) -> VideoResult:
        """Render ``spec`` to a clip. Raises ``LiveVideoDisabled`` when gated off."""
        ...

    async def healthy(self) -> bool:
        """Cheap liveness probe (no render spend); ``True`` when routable."""
        ...


def select_backend(backends: Sequence[OpenVideoBackend], spec: WanSpec) -> OpenVideoBackend | None:
    """Return the first backend whose capabilities support ``spec`` (pure helper)."""
    for backend in backends:
        if backend.capabilities().supports(spec):
            return backend
    return None
