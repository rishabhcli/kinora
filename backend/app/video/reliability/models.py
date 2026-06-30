"""Domain types for the end-to-end render reliability coordinator.

These are the *public vocabulary* of :mod:`app.video.reliability`: the shot to
render, the outcome the caller ships, and the structured attempt log that records
**every** provider tried and exactly why each one failed, was rejected, or won.
All pydantic v2, frozen where it makes sense, network- and infra-free.

The state vocabulary tracks the §9.7 per-shot machine
(Rendering → QA → Repair/Degraded → Accepted): an attempt may fail to *produce*
a clip (provider/router error, budget abort, deadline), or it may produce one that
the quality gate *rejects* (a wrong face is a fail even if the scene is pretty,
§10 Critic) — the coordinator escalates to the next-best provider rather than
shipping garbage, and only falls back to a degraded-but-real result when the
candidate list is exhausted. It **never** silently returns nothing.
"""

from __future__ import annotations

import enum

from pydantic import BaseModel, ConfigDict, Field


class RenderTier(enum.IntEnum):
    """Fidelity tiers of a render result, high → low (the §12.4 ladder).

    Ordered so ``a > b`` means "strictly more fidelity"; the coordinator's
    best-so-far selection and graceful fallback compare on this.
    """

    FULL_VIDEO = 40
    """A real generated video clip (Wan/MiniMax) that passed QA."""
    ANIMATIC = 30
    """Ken-Burns / animatic motion over a real keyframe image."""
    KEYFRAME = 20
    """A single still keyframe (the book's own or a generated one)."""
    NARRATED_TEXT = 10
    """The bottom rung: a narrated text card. Real, always available."""


class AttemptStatus(enum.StrEnum):
    """Why a single provider attempt ended (one row of the attempt log)."""

    ACCEPTED = "accepted"
    """Produced a clip that passed the quality gate. Terminal win."""
    PROVIDER_ERROR = "provider_error"
    """The router/provider raised (timeout, 5xx, connection reset, bad request)."""
    QUALITY_REJECTED = "quality_rejected"
    """Produced a clip but the quality score was below the gate → escalate."""
    BUDGET_DENIED = "budget_denied"
    """The cost budget could not reserve this attempt's spend → abort the shot."""
    GOVERNOR_BLOCKED = "governor_blocked"
    """The capacity/SLA governor admitted no providers (or all were shed)."""
    DEADLINE_EXCEEDED = "deadline_exceeded"
    """The per-shot deadline elapsed before this provider could be attempted."""
    SKIPPED_NO_BUDGET_HEADROOM = "skipped_no_budget_headroom"
    """Pre-flight: the provider's estimated cost alone exceeds remaining budget."""


class FallbackReason(enum.StrEnum):
    """Why the coordinator fell back to a degraded result instead of full video."""

    NONE = "none"
    """No fallback — a candidate produced an accepted full result."""
    ALL_PROVIDERS_FAILED = "all_providers_failed"
    """Every ranked candidate errored, was rejected, or skipped."""
    BUDGET_EXHAUSTED = "budget_exhausted"
    """The budget was denied / drained before any candidate could ship video."""
    DEADLINE_EXCEEDED = "deadline_exceeded"
    """The per-shot deadline elapsed; shipped the best-so-far or a degraded card."""
    NO_CANDIDATES = "no_candidates"
    """The governor + budget pre-flight admitted no provider at all."""
    QUALITY_FLOOR = "quality_floor"
    """Candidates produced clips but none cleared the quality gate."""


class ShotSpec(BaseModel):
    """The unit of work: render *this* shot reliably.

    Deliberately minimal — the coordinator only needs the identity (for logging /
    idempotency), the budgeted size (video-seconds drive the cost reservation),
    the per-shot deadline, and the quality floor. The rich §10 shot spec
    (prompt, refs, camera, seed) is the router/provider's concern, carried opaque.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    shot_id: str = Field(..., min_length=1, description="Stable per-shot identity.")
    book_id: str | None = Field(default=None, description="Owning book, for telemetry.")
    est_video_seconds: float = Field(
        default=5.0, gt=0, description="Budgeted screen-time of this shot (§4)."
    )
    deadline_s: float = Field(
        default=30.0,
        gt=0,
        description="Wall-budget before the reader arrives; returns best-so-far at expiry.",
    )
    min_quality: float = Field(
        default=0.6,
        ge=0.0,
        le=1.0,
        description="Quality-gate floor in [0,1]; below it a clip is rejected (§10).",
    )
    payload: dict[str, object] = Field(
        default_factory=dict, description="Opaque shot spec passed to the router verbatim."
    )


class RenderResult(BaseModel):
    """A concrete artifact a provider produced (or a degraded fallback card).

    ``tier`` places it on the §12.4 ladder; ``quality`` is the gate score in
    [0,1] (1.0 for a synthesized degraded card, which is trivially "valid" at its
    own tier). ``uri`` is where the artifact lives (object storage / a data-uri
    for a text card); the coordinator never opens it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    shot_id: str
    provider: str = Field(..., min_length=1)
    tier: RenderTier = RenderTier.FULL_VIDEO
    uri: str = Field(..., min_length=1)
    quality: float = Field(default=0.0, ge=0.0, le=1.0)
    cost_usd: float = Field(default=0.0, ge=0.0)
    video_seconds: float = Field(default=0.0, ge=0.0)
    degraded: bool = Field(
        default=False, description="True when this is a graceful fallback, not a real take."
    )


class AttemptRecord(BaseModel):
    """One row of the structured attempt log: a single provider attempt.

    Captures the ranked position, what happened, the wall-clock window it ran in
    (relative to the shot's t0), the spend it reserved/charged, and a one-line
    human ``detail`` — enough for an operator to reconstruct *why* the coordinator
    moved on from this provider.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    rank: int = Field(..., ge=0, description="0-based position in the ranked candidate list.")
    provider: str
    status: AttemptStatus
    started_at_s: float = Field(..., ge=0.0, description="Seconds since shot t0.")
    ended_at_s: float = Field(..., ge=0.0)
    attempts_used: int = Field(
        default=1, ge=0, description="Router-level tries/hedges spent on this provider."
    )
    quality: float | None = Field(default=None, ge=0.0, le=1.0)
    cost_reserved_usd: float = Field(default=0.0, ge=0.0)
    cost_charged_usd: float = Field(default=0.0, ge=0.0)
    detail: str = Field(default="", description="One-line human reason, never a secret.")

    @property
    def elapsed_s(self) -> float:
        return max(0.0, self.ended_at_s - self.started_at_s)


class RenderOutcome(BaseModel):
    """The terminal answer of :meth:`ReliableRenderCoordinator.render`.

    ``ok`` is True when a real artifact ships (full *or* a degraded-but-real
    fallback) — it is **never** False with a missing result. ``result`` is the
    chosen artifact; ``fallback_reason`` explains any drop below full video; and
    ``log`` is the complete, ordered :class:`RenderAttemptLog` for observability.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    ok: bool
    result: RenderResult
    fallback_reason: FallbackReason = FallbackReason.NONE
    log: RenderAttemptLog

    @property
    def degraded(self) -> bool:
        return self.result.degraded


class RenderAttemptLog(BaseModel):
    """The full, ordered record of an end-to-end render (every provider tried).

    The coordinator appends one :class:`AttemptRecord` per provider considered, in
    the order it considered them, then stamps the totals. This is the single
    observability surface — emit it to logs/metrics and you can answer "why did
    shot X take this path?" without re-running anything.
    """

    model_config = ConfigDict(extra="forbid")

    shot_id: str
    ranked_providers: list[str] = Field(default_factory=list)
    attempts: list[AttemptRecord] = Field(default_factory=list)
    total_elapsed_s: float = Field(default=0.0, ge=0.0)
    total_cost_charged_usd: float = Field(default=0.0, ge=0.0)
    deadline_s: float = Field(default=0.0, ge=0.0)
    final_status: AttemptStatus | None = Field(default=None)
    final_tier: RenderTier | None = Field(default=None)
    fallback_reason: FallbackReason = FallbackReason.NONE

    def add(self, record: AttemptRecord) -> None:
        self.attempts.append(record)

    @property
    def providers_tried(self) -> list[str]:
        """Distinct providers that actually ran an attempt, in order."""
        seen: list[str] = []
        for rec in self.attempts:
            if rec.provider not in seen:
                seen.append(rec.provider)
        return seen


# Resolve the forward references between the co-recursive models.
RenderOutcome.model_rebuild()


__all__ = [
    "AttemptRecord",
    "AttemptStatus",
    "FallbackReason",
    "RenderAttemptLog",
    "RenderOutcome",
    "RenderResult",
    "RenderTier",
    "ShotSpec",
]
