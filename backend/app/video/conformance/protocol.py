"""The local provider contract the conformance harness verifies.

This module owns a *self-contained* description of what a video adapter must
expose to be trusted by Kinora's render pipeline. It deliberately mirrors the
shared :class:`app.providers.video_router.VideoBackend` protocol (``name`` /
``render`` / ``healthy``) and the hosted submit→poll→fetch lifecycle of
:class:`app.providers.video.VideoProvider`, but does **not** import them as a
hard dependency: the harness must be able to run against *any* adapter — present
or future, from any model family — without being coupled to one concrete
provider.

The contract is split into two pieces:

* :class:`ConformantVideoProvider` — the runtime ``Protocol`` an adapter must
  satisfy. The minimum surface is ``name`` + ``capabilities()`` + ``render()``;
  the staged ``submit()`` / ``poll()`` / ``fetch()`` / ``cancel()`` members are
  *optional* (declared on the protocol but not all required), because some
  adapters expose only the one-shot ``render`` façade while others surface the
  async lifecycle the scheduler can drive directly. The harness probes which
  members an adapter actually implements (via :func:`provider_surface`) and only
  runs the checks that apply.
* :class:`VideoCapabilities` — the **honest, machine-checkable** declaration of
  what the adapter supports: modes, duration bounds, resolutions, and lifecycle
  affordances. Every claim here is verified against behaviour by the harness;
  an adapter that claims a mode it cannot actually render fails
  :data:`~app.video.conformance.report.ConformanceCheck.CAPABILITY_HONESTY`.

Why a local Protocol rather than importing ``VideoBackend``? Three reasons:

1. **No hard block.** Sibling agents are concurrently reshaping
   ``app/video/abstraction`` and the provider layer; binding to their in-flight
   types would couple this harness to a moving target.
2. **Richer than the router needs.** The router only needs ``render``; trust
   needs capabilities, lifecycle, and asset semantics — a superset.
3. **Structural, not nominal.** Because it is a ``runtime_checkable`` Protocol,
   *any* object with the right shape conforms — including the real
   :class:`VideoProvider`, the :class:`MiniMaxVideoProvider`, a future
   self-hosted lane, or one of the test fakes in :mod:`.fakes`.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from app.providers.types import VideoResult, WanMode, WanSpec

# --------------------------------------------------------------------------- #
# Capability declaration (the contract the adapter promises to honour)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class DurationBounds:
    """The closed duration window an adapter claims to support, in seconds.

    A claim of ``min_s=5, max_s=15`` asserts the adapter renders *every* integer
    duration in ``[5, 15]``. The harness picks representative durations inside
    and just outside the window and verifies the adapter accepts the former and
    rejects the latter (a :class:`app.providers.errors.ProviderBadRequest`).
    """

    min_s: int = 5
    max_s: int = 5

    def __post_init__(self) -> None:
        if self.min_s <= 0 or self.max_s <= 0:
            raise ValueError("duration bounds must be positive")
        if self.min_s > self.max_s:
            raise ValueError("duration min_s must be <= max_s")

    def contains(self, duration_s: int) -> bool:
        """True when ``duration_s`` is within the declared window."""
        return self.min_s <= duration_s <= self.max_s

    def representative_inside(self) -> int:
        """A duration the adapter must accept (the window midpoint, floored)."""
        return (self.min_s + self.max_s) // 2

    def just_below(self) -> int | None:
        """A duration the adapter must reject below the window (or ``None``)."""
        return self.min_s - 1 if self.min_s > 1 else None

    def just_above(self) -> int:
        """A duration the adapter must reject above the window."""
        return self.max_s + 1


@dataclass(frozen=True, slots=True)
class VideoCapabilities:
    """An adapter's HONEST, machine-checkable declaration of what it supports.

    Every field is a *promise* the harness verifies against behaviour, scripting
    a fake transport to the declared profile and asserting the adapter actually
    delivers. The cardinal sin in this subsystem is a capability claim the
    adapter cannot back up — that fails capability-honesty, the whole point of
    the suite.

    Attributes:
        provider_id: Stable identity (matches the adapter's ``name``).
        modes: The :class:`~app.providers.types.WanMode` s the adapter renders.
        durations: The duration window (seconds) the adapter accepts.
        resolutions: The resolution labels the adapter accepts (e.g. ``720P``).
        supports_seed: The adapter honours a deterministic ``seed``.
        supports_negative_prompt: The adapter accepts a negative prompt.
        eager_download: ``render`` returns clip *bytes*, not just an expiring URL
            (the render pipeline relies on this — task URLs expire, §9.7).
        extracts_last_frame: ``render`` returns ``last_frame_bytes`` for
            continuation chaining.
        staged_lifecycle: The adapter exposes ``submit``/``poll``/``fetch`` for
            the scheduler to drive directly (not only the one-shot ``render``).
        cancellable: The adapter supports cancelling an in-flight task.
        idempotent_submit: Re-submitting the *same* spec (by ``shot_id``) returns
            the same task rather than double-spending.
    """

    provider_id: str
    modes: frozenset[WanMode] = field(default_factory=lambda: frozenset({WanMode.TEXT_TO_VIDEO}))
    durations: DurationBounds = field(default_factory=DurationBounds)
    resolutions: frozenset[str] = field(default_factory=lambda: frozenset({"720P"}))
    supports_seed: bool = False
    supports_negative_prompt: bool = False
    eager_download: bool = True
    extracts_last_frame: bool = False
    staged_lifecycle: bool = False
    cancellable: bool = False
    idempotent_submit: bool = False

    def supports_mode(self, mode: WanMode) -> bool:
        return mode in self.modes

    def supports_resolution(self, resolution: str) -> bool:
        return resolution in self.resolutions

    def example_mode(self) -> WanMode:
        """A mode the adapter claims (deterministic: the lowest-valued one)."""
        if not self.modes:
            raise ValueError(f"{self.provider_id} declares no modes")
        return sorted(self.modes, key=lambda m: m.value)[0]

    def example_resolution(self) -> str:
        if not self.resolutions:
            raise ValueError(f"{self.provider_id} declares no resolutions")
        return sorted(self.resolutions)[0]


# --------------------------------------------------------------------------- #
# Staged async lifecycle handle (submit → poll → fetch)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class SubmittedTask:
    """The handle returned by a staged ``submit`` — what ``poll``/``fetch`` need."""

    task_id: str
    #: The model id the task was submitted against (telemetry / fetch routing).
    model: str
    #: Echoed back so idempotency can be asserted without re-deriving the key.
    shot_id: str | None = None


@dataclass(frozen=True, slots=True)
class TaskStatus:
    """A single ``poll`` observation of an in-flight task."""

    task_id: str
    #: One of ``pending`` / ``running`` / ``succeeded`` / ``failed`` / ``canceled``.
    state: str
    #: The result URL once ``succeeded`` (still expiring — ``fetch`` must download).
    clip_url: str | None = None
    message: str | None = None

    @property
    def is_terminal(self) -> bool:
        return self.state in ("succeeded", "failed", "canceled")

    @property
    def is_success(self) -> bool:
        return self.state == "succeeded"


# --------------------------------------------------------------------------- #
# The provider protocol the harness verifies
# --------------------------------------------------------------------------- #


@runtime_checkable
class ConformantVideoProvider(Protocol):
    """The contract a video adapter must satisfy to be trusted.

    The required surface is ``name`` + ``capabilities()`` + ``render()`` — the
    same one-shot façade the router drives. The staged lifecycle members
    (``submit`` / ``poll`` / ``fetch`` / ``cancel``) are *optional*: an adapter
    that sets :attr:`VideoCapabilities.staged_lifecycle` must implement them and
    the harness will exercise them; one that doesn't is checked only on the
    one-shot path. Optionality is discovered structurally via
    :func:`provider_surface`, never assumed.
    """

    #: Stable identity for telemetry + health bookkeeping (router-compatible).
    name: str

    def capabilities(self) -> VideoCapabilities:
        """The adapter's honest capability declaration (sync; no I/O)."""
        ...

    async def render(self, spec: WanSpec) -> VideoResult:
        """One-shot render. Raises ``LiveVideoDisabled`` when the gate is off."""
        ...

    # -- optional staged lifecycle (present iff capabilities.staged_lifecycle) -

    async def submit(self, spec: WanSpec) -> SubmittedTask:
        """Submit a render task without waiting for it (optional)."""
        ...

    async def poll(self, task: SubmittedTask) -> TaskStatus:
        """Observe an in-flight task's status (optional)."""
        ...

    async def fetch(self, task: SubmittedTask) -> VideoResult:
        """Download the completed clip's bytes (optional; eager download)."""
        ...

    async def cancel(self, task: SubmittedTask) -> None:
        """Cancel an in-flight task (optional; present iff ``cancellable``)."""
        ...


@dataclass(frozen=True, slots=True)
class ProviderSurface:
    """Which optional members an adapter actually implements (structural probe)."""

    has_render: bool
    has_capabilities: bool
    has_submit: bool
    has_poll: bool
    has_fetch: bool
    has_cancel: bool

    @property
    def has_staged_lifecycle(self) -> bool:
        """True when the full submit→poll→fetch chain is callable."""
        return self.has_submit and self.has_poll and self.has_fetch


def _is_async_callable(obj: object, attr: str) -> bool:
    """True when ``obj.attr`` exists and is callable (async checked at call)."""
    member = getattr(obj, attr, None)
    return callable(member)


def provider_surface(provider: object) -> ProviderSurface:
    """Discover which contract members ``provider`` implements (no calls made)."""
    return ProviderSurface(
        has_render=_is_async_callable(provider, "render"),
        has_capabilities=_is_async_callable(provider, "capabilities"),
        has_submit=_is_async_callable(provider, "submit"),
        has_poll=_is_async_callable(provider, "poll"),
        has_fetch=_is_async_callable(provider, "fetch"),
        has_cancel=_is_async_callable(provider, "cancel"),
    )


def all_modes() -> Sequence[WanMode]:
    """Every Wan mode, in declaration order (for exhaustive capability sweeps)."""
    return tuple(WanMode)


__all__ = [
    "ConformantVideoProvider",
    "DurationBounds",
    "ProviderSurface",
    "SubmittedTask",
    "TaskStatus",
    "VideoCapabilities",
    "all_modes",
    "provider_surface",
]
