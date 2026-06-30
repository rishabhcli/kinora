"""Injectable seams + local domain types for the shadow / live-eval harness.

This module is the *contract surface* of :mod:`app.video.shadow`. Everything the
harness touches that could reach the network, a clock, or randomness is behind a
small, ``runtime_checkable`` :class:`typing.Protocol` here so the production
orchestrator can wire the real provider / quality-scorer / sampler / clock and
the tests can inject deterministic fakes.

LOCAL PROTOCOLS — by design
---------------------------
Rounds 1 & 2 of this marathon are *not* merged, so we cannot import their quality
scorer, router, or job packages. Instead we declare the **minimal** structural
contracts the harness needs:

* :class:`VideoRenderProvider` — submit one :class:`ShotSpec`, get a
  :class:`RenderOutcome` (clip ref, measured cost + latency, or a typed failure).
  The real :class:`app.providers.video.VideoProvider` / ``VideoRouter`` adapts to
  this with a thin shim the orchestrator owns; we never import it here.
* :class:`QualityScorer` — score a finished render in ``[0, 1]`` (higher better).
  The real §13 metric stack adapts to this; the harness only needs the float.
* :class:`Sampler` — decide, deterministically per shot, whether this request is
  in the shadow sample (no global RNG; keyed by ``shot_id``).
* :class:`Clock` — monotonic time source so latency + budget windows are testable
  without ``time.monotonic``.

The harness depends only on these protocols, never on concrete provider/score
types, so it is import-clean with no infra and trivially faked in unit tests.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

# --------------------------------------------------------------------------- #
# Local domain types — minimal, self-contained, pydantic v2.
# --------------------------------------------------------------------------- #


class ShotSpec(BaseModel):
    """A model-agnostic description of one shot to render.

    Deliberately decoupled from the provider layer's ``WanSpec`` so the harness
    can replay historical shots and compare *across* model families. The
    orchestrator translates a ``WanSpec`` (or any future spec) into this when it
    forks a shadow render; the candidate provider shim translates back.

    ``shot_id`` is the stable key for sampling, pairing, and replay determinism —
    two renders of "the same shot" (production + candidate) share it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    shot_id: str
    #: Coarse render mode label (e.g. ``"text_to_video"``); free-form so this type
    #: never has to track the provider's ``WanMode`` enum.
    mode: str = "text_to_video"
    prompt: str = ""
    negative_prompt: str | None = None
    #: Requested clip length in seconds — drives the *expected* video-seconds spend.
    duration_s: float = 5.0
    resolution: str = "720P"
    seed: int | None = None
    #: Opaque, JSON-safe extras (reference URLs, book/scene ids …) carried through
    #: replay so a recorded spec round-trips byte-for-byte.
    inputs: Mapping[str, str] = Field(default_factory=dict)

    @property
    def expected_video_seconds(self) -> float:
        """Video-seconds this shot is expected to bill against the eval budget."""
        return max(0.0, float(self.duration_s))


class FailureKind(StrEnum):
    """Why a render did not produce a usable clip.

    ``GATED`` is special: it is *not* a fault. It means the live-video spend gate
    (or the eval budget) deliberately refused the render — the harness records it
    as a non-event, never as a candidate quality/availability strike.
    """

    NONE = "none"
    GATED = "gated"
    TIMEOUT = "timeout"
    PROVIDER_ERROR = "provider_error"
    BAD_REQUEST = "bad_request"
    BUDGET_EXHAUSTED = "budget_exhausted"


class RenderOutcome(BaseModel):
    """The result of one render attempt on one model — success or typed failure.

    Carries exactly what the comparison needs: success flag, a measured quality
    score (``None`` until scored), the *measured* cost (video-seconds) and latency
    (ms), and a failure classification. Never carries clip bytes — only a ref.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    model: str
    succeeded: bool
    failure: FailureKind = FailureKind.NONE
    clip_ref: str | None = None
    #: Quality in ``[0, 1]``; ``None`` when the render failed or is not yet scored.
    quality: float | None = None
    #: Measured video-seconds actually billed by this render (the scarce currency).
    video_seconds: float = 0.0
    latency_ms: float = 0.0
    #: Provider/task identifier for cross-referencing telemetry.
    request_id: str | None = None

    @property
    def is_gated(self) -> bool:
        """True when the render was refused by a spend gate (a non-fault)."""
        return self.failure is FailureKind.GATED


# --------------------------------------------------------------------------- #
# Injectable seams.
# --------------------------------------------------------------------------- #


@runtime_checkable
class VideoRenderProvider(Protocol):
    """A model the harness can render one shot on, off the critical path.

    Both the *production* model (for the paired reference render, when the
    orchestrator chooses to re-render rather than reuse the live result) and the
    *candidate* model implement this. Implementations MUST be side-effect-isolated
    from the reader's experience: a shadow render must never mutate canon, the
    reader budget, or the served clip.
    """

    @property
    def model_id(self) -> str:
        """Stable identity of the underlying model (for telemetry + reports)."""
        ...

    async def render(self, spec: ShotSpec) -> RenderOutcome:
        """Render ``spec`` and return a typed outcome (success or failure).

        MUST NOT raise for ordinary provider faults / gating — surface them as a
        :class:`RenderOutcome` with the right :class:`FailureKind` so the collector
        records the comparison. May raise only for genuine programmer errors.
        """
        ...


@runtime_checkable
class QualityScorer(Protocol):
    """Scores a finished render in ``[0, 1]`` (higher = better)."""

    async def score(self, spec: ShotSpec, outcome: RenderOutcome) -> float:
        """Return the quality of ``outcome`` for ``spec`` in ``[0, 1]``."""
        ...


@runtime_checkable
class Sampler(Protocol):
    """Deterministic per-shot membership in the shadow sample.

    Keyed by ``shot_id`` (not a global RNG) so the same shot always makes the same
    decision — essential for reproducible eval runs and for not double-sampling a
    shot that is retried.
    """

    def in_sample(self, shot_id: str) -> bool:
        """True iff this shot should also be rendered on the candidate."""
        ...


@runtime_checkable
class Clock(Protocol):
    """A monotonic seconds source for latency + budget-window measurement."""

    def monotonic(self) -> float:
        """Monotonic time in seconds (only differences are meaningful)."""
        ...


__all__ = [
    "Clock",
    "FailureKind",
    "QualityScorer",
    "RenderOutcome",
    "Sampler",
    "ShotSpec",
    "VideoRenderProvider",
]
