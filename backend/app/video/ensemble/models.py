"""Value objects for multi-model best-of-N rendering (pydantic v2, frozen, pure).

Everything here is data: the inputs to a fan-out (``ShotRenderSpec`` + the set of
``ProviderChoice`` s + an ``EnsembleConfig``), the per-candidate outcome record
(``Candidate``: its scored output, cost, and disposition), and the emitted
``SelectionReport`` that explains *why the winner won*. No I/O, no model calls — so the
selection logic over these is exhaustively unit-testable.

Scores are normalized 0..1 "goodness" (1 = ideal) so a single sign convention holds.
Cost is expressed in **video-seconds** (the scarce, hard-capped §11 resource) and an
optional **USD** estimate; objectives that divide by cost use video-seconds by default
(seconds are what the budget meters) but can be told to use USD.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

if TYPE_CHECKING:
    from app.core.config import Settings

# --------------------------------------------------------------------------- #
# Render I/O (structural shims over the provider layer)
# --------------------------------------------------------------------------- #


class ShotRenderSpec(BaseModel):
    """A request to render one shot, fanned out across providers unchanged.

    Deliberately minimal: the ensemble passes this opaquely to every provider's
    ``render`` (a real wiring would pass the provider's native ``WanSpec``). The
    fields here are only what the ensemble itself reads — duration to size the budget
    reservation, the shot id for telemetry, the tier for the fan-out enable gate, and
    a locked-identity reference key for the consistency-vote mode.
    """

    model_config = ConfigDict(extra="allow", frozen=True)

    shot_id: str
    #: Nominal clip length; the per-candidate budget reservation is sized from this.
    duration_s: float = 5.0
    #: Quality tier of this shot (e.g. ``"hero"`` / ``"standard"``); best-of-N only
    #: runs for tiers the config explicitly enables (default: none → no fan-out).
    tier: str = "standard"
    #: Locked appearance/identity key, for consistency-vote attribution (optional).
    identity_key: str | None = None


class RenderOutput(BaseModel):
    """The product of one provider render — a structural shim over ``VideoResult``.

    The ensemble only needs the originating model id and a reference to the clip; it
    never decodes pixels itself (the :class:`QualityScorer` does). ``clip_ref`` is an
    opaque handle (a URL, a bytes object, an object-store key) passed to the scorer.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)

    model: str
    duration_s: float
    clip_ref: Any = None
    provider_task_id: str | None = None


class QualityScore(BaseModel):
    """A normalized 0..1 quality judgement + the §9.5 per-axis breakdown.

    ``composite`` is the headline the objectives rank by. The sub-scores (all 0..1
    goodness — identity high = on-model, style/motion already inverted to goodness,
    timeline 1/0) let the consistency vote rank by identity alone and let the report
    explain a win. ``passed`` records whether the hard gate would accept (advisory to
    the report; the ensemble ranks even gate-failing candidates so it can pick the
    *least bad* when nothing passes).
    """

    model_config = ConfigDict(frozen=True)

    composite: float = Field(ge=0.0, le=1.0)
    identity: float = Field(default=1.0, ge=0.0, le=1.0)
    style: float = Field(default=1.0, ge=0.0, le=1.0)
    timeline: float = Field(default=1.0, ge=0.0, le=1.0)
    motion: float = Field(default=1.0, ge=0.0, le=1.0)
    passed: bool = True


# --------------------------------------------------------------------------- #
# Budget shim
# --------------------------------------------------------------------------- #


class BudgetReservation(BaseModel):
    """A handle to an outstanding budget earmark (shim over ``Reservation``)."""

    model_config = ConfigDict(frozen=True)

    id: str
    video_seconds: float


# --------------------------------------------------------------------------- #
# Fan-out inputs
# --------------------------------------------------------------------------- #


class ProviderChoice(BaseModel):
    """One provider enrolled in a fan-out, with its cost position.

    ``cost_per_s`` is the relative cost of one video-second on this provider (turbo <
    quality); absolute units don't matter to quality-per-dollar, only ratios.
    ``usd_per_s`` is an optional real-money estimate for USD-denominated objectives /
    cost caps. ``priority`` orders launches (lower = launched first) so early-stop and
    a single deterministic fan-out order are honoured.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    cost_per_s: float = Field(default=1.0, gt=0.0)
    usd_per_s: float = Field(default=0.0, ge=0.0)
    #: Launch order (lower first). Ties break on ``name`` for determinism.
    priority: int = 0


class Objective(StrEnum):
    """How the winner is selected from the scored candidates."""

    #: Highest composite quality wins (cost ignored).
    MAX_QUALITY = "max_quality"
    #: Highest quality-per-unit-cost wins (composite / cost). Best value.
    QUALITY_PER_COST = "quality_per_cost"
    #: Highest quality among candidates within a hard per-shot cost cap.
    QUALITY_UNDER_COST_CAP = "quality_under_cost_cap"
    #: Most consistent with the locked identity (rank by the identity sub-score,
    #: tie-broken by composite). For hero shots where on-model trumps everything.
    CONSISTENCY_VOTE = "consistency_vote"


class CostUnit(StrEnum):
    """Which cost figure objectives / caps divide by."""

    VIDEO_SECONDS = "video_seconds"
    USD = "usd"


class EnsembleConfig(BaseModel):
    """Tunables for one best-of-N run (deterministic; no env reads).

    Defaults are **conservative on purpose**: ``enabled=False`` and ``max_candidates=1``
    so an accidental wiring NEVER fans out or overspends. Best-of-N only happens when a
    caller explicitly enables it *and* the shot's tier is in ``enabled_tiers``.
    """

    model_config = ConfigDict(frozen=True)

    #: Master switch. False (default) → degrade to a single best-priority render.
    enabled: bool = False
    #: Tiers for which fan-out is permitted. Empty (default) → no tier fans out.
    enabled_tiers: frozenset[str] = frozenset()
    #: Max providers to launch for one shot. 1 (default) → no fan-out even if enabled.
    max_candidates: int = 1
    #: Max providers running concurrently (the rest queue). Bounds peak fan-out.
    max_concurrency: int = 2
    #: Selection objective.
    objective: Objective = Objective.MAX_QUALITY
    #: Cost figure for quality-per-cost / the cost cap.
    cost_unit: CostUnit = CostUnit.VIDEO_SECONDS
    #: Hard per-shot cost cap (in ``cost_unit``). 0 → no cap. For
    #: QUALITY_UNDER_COST_CAP a candidate over the cap is excluded from selection;
    #: for every objective the guard refuses to *launch* a candidate that would
    #: push committed+reserved spend over this cap.
    per_shot_cost_cap: float = 0.0
    #: Early-stop: stop launching more once a scored candidate's composite clears
    #: this. 0 or >1 → never early-stop (run the full fan-out). Losers in flight are
    #: cancelled when a good-enough candidate lands.
    good_enough_quality: float = 0.0
    #: Minimum quality improvement a higher-cost candidate must show over the
    #: cheapest acceptable one to be preferred under QUALITY_PER_COST tie handling.
    min_quality_margin: float = 1e-9

    @model_validator(mode="after")
    def _check_bounds(self) -> EnsembleConfig:
        if self.max_candidates < 1:
            raise ValueError("max_candidates must be >= 1")
        if self.max_concurrency < 1:
            raise ValueError("max_concurrency must be >= 1")
        if self.per_shot_cost_cap < 0:
            raise ValueError("per_shot_cost_cap must be >= 0")
        return self

    @classmethod
    def from_settings(cls, settings: Settings) -> EnsembleConfig:
        """Build a run config from application :class:`Settings` (env-overridable).

        Parses the comma-separated tier list and the string objective/cost-unit
        enums. Invalid enum strings raise here (fail fast at construction) rather
        than silently degrading to a default that might over- or under-spend.
        """
        tiers = frozenset(
            t.strip() for t in settings.ensemble_enabled_tiers.split(",") if t.strip()
        )
        return cls(
            enabled=settings.ensemble_enabled,
            enabled_tiers=tiers,
            max_candidates=settings.ensemble_max_candidates,
            max_concurrency=settings.ensemble_max_concurrency,
            objective=Objective(settings.ensemble_objective),
            cost_unit=CostUnit(settings.ensemble_cost_unit),
            per_shot_cost_cap=settings.ensemble_per_shot_cost_cap,
            good_enough_quality=settings.ensemble_good_enough_quality,
        )


# --------------------------------------------------------------------------- #
# Outcomes
# --------------------------------------------------------------------------- #


class CandidateStatus(StrEnum):
    """The disposition of one fanned-out candidate."""

    SCORED = "scored"  # rendered + scored successfully
    FAILED = "failed"  # provider render raised
    SCORE_FAILED = "score_failed"  # rendered but scoring raised
    CANCELLED = "cancelled"  # cancelled by early-stop before completing
    SKIPPED = "skipped"  # never launched (cap/budget guard refused)
    OVER_CAP = "over_cap"  # scored but excluded by the cost cap


class Candidate(BaseModel):
    """The full record of one provider's attempt at the shot."""

    model_config = ConfigDict(frozen=True)

    provider: str
    status: CandidateStatus
    #: Launch order index (the deterministic fan-out order). Lower launched first.
    order: int
    output: RenderOutput | None = None
    score: QualityScore | None = None
    #: Cost actually attributable to this candidate.
    video_seconds: float = 0.0
    usd: float = 0.0
    #: Human-readable note (e.g. the exception type, or why it was skipped).
    detail: str = ""

    @property
    def is_eligible(self) -> bool:
        """True when this candidate has a usable score (a selection contender)."""
        return self.status is CandidateStatus.SCORED and self.score is not None

    def cost_in(self, unit: CostUnit) -> float:
        """This candidate's cost in the requested unit."""
        return self.usd if unit is CostUnit.USD else self.video_seconds


class SelectionReport(BaseModel):
    """The emitted explanation of a best-of-N run — every candidate + why the winner won.

    Built once at the end of a run. ``winner`` is the chosen provider name (or ``None``
    when nothing was eligible). The objective, the per-candidate scores/costs/statuses,
    and the early-stop / cost-cap flags are all here so the decision is fully auditable
    (and the §13 honesty story holds: nothing is hidden).
    """

    model_config = ConfigDict(frozen=True)

    shot_id: str
    objective: Objective
    cost_unit: CostUnit
    enabled: bool
    winner: str | None
    candidates: list[Candidate]
    #: True when early-stop fired (a good-enough candidate cancelled the rest).
    early_stopped: bool = False
    #: True when fan-out was suppressed (disabled / tier not enabled / max=1).
    fanned_out: bool = False
    #: The winner's score for at-a-glance reporting.
    winning_score: float | None = None
    #: Total cost charged (winner only; losers are released).
    charged_video_seconds: float = 0.0
    charged_usd: float = 0.0
    #: Free-text reason the winner beat the field.
    reason: str = ""

    @property
    def eligible(self) -> list[Candidate]:
        """Candidates that produced a usable score."""
        return [c for c in self.candidates if c.is_eligible]

    def as_log_fields(self) -> dict[str, Any]:
        """Structured-log-safe summary (no clip bytes, no prompt content)."""
        return {
            "shot_id": self.shot_id,
            "objective": str(self.objective),
            "enabled": self.enabled,
            "fanned_out": self.fanned_out,
            "winner": self.winner,
            "winning_score": (
                round(self.winning_score, 4) if self.winning_score is not None else None
            ),
            "candidates": len(self.candidates),
            "eligible": len(self.eligible),
            "early_stopped": self.early_stopped,
            "charged_video_seconds": round(self.charged_video_seconds, 3),
            "reason": self.reason,
        }


__all__ = [
    "BudgetReservation",
    "Candidate",
    "CandidateStatus",
    "CostUnit",
    "EnsembleConfig",
    "Objective",
    "ProviderChoice",
    "QualityScore",
    "RenderOutput",
    "SelectionReport",
    "ShotRenderSpec",
]
