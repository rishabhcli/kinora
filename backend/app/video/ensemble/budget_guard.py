"""The strict budget guard for best-of-N — fail-closed by default (§11).

Best-of-N renders the *same shot* on K models, so it can spend up to K× a normal shot.
That is a deliberate, expensive choice for hero shots only. This guard is the gate that
makes it safe: it refuses to fan out unless **every** condition holds, and it refuses to
launch any single candidate that would push the per-shot spend past the configured cap.

The guard is *pure policy over a budget ledger* — it wraps a
:class:`~app.video.ensemble.protocols.MultiRenderBudget` (the scarce video-seconds
ledger, structurally the real ``BudgetService``) and adds the ensemble's own per-shot
fan-out accounting on top. It never reads env directly; all knobs come from the
:class:`~app.video.ensemble.models.EnsembleConfig`.

Refusal cascade (all must pass to fan out):

#. ``config.enabled`` is True — best-of-N is off by default.
#. ``shot.tier`` is in ``config.enabled_tiers`` — only enabled tiers fan out.
#. ``config.max_candidates`` > 1 — one candidate is not an ensemble.
#. ``budget.can_render_live()`` — the hard ``KINORA_LIVE_VIDEO`` go-live gate.

When fan-out is refused, the renderer degrades to a **single** best-priority render
(still gated by ``can_render_live`` for the live spend). The guard NEVER fabricates a
clip and NEVER reserves seconds it hasn't been asked to.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from .models import (
    BudgetReservation,
    CostUnit,
    EnsembleConfig,
    ProviderChoice,
    ShotRenderSpec,
)
from .protocols import MultiRenderBudget


class FanOutRefusal(StrEnum):
    """Why fan-out was refused (degrade to a single render)."""

    DISABLED = "disabled"  # config.enabled is False
    TIER_NOT_ENABLED = "tier_not_enabled"  # shot.tier not in enabled_tiers
    SINGLE_CANDIDATE = "single_candidate"  # max_candidates <= 1
    LIVE_GATE_OFF = "live_gate_off"  # can_render_live() is False
    ALLOWED = "allowed"  # fan-out permitted


@dataclass(frozen=True, slots=True)
class FanOutDecision:
    """The guard's verdict on whether a shot may fan out."""

    allowed: bool
    refusal: FanOutRefusal

    @property
    def reason(self) -> str:
        return self.refusal.value


class CostCapExceeded(RuntimeError):  # noqa: N818 - mirrors BudgetExceeded naming
    """Raised when launching a candidate would breach the per-shot cost cap."""

    def __init__(self, *, requested: float, committed: float, cap: float, unit: CostUnit) -> None:
        self.requested = requested
        self.committed = committed
        self.cap = cap
        self.unit = unit
        super().__init__(
            f"per-shot cost cap exceeded ({unit.value}): requested {requested:.3f} "
            f"+ committed {committed:.3f} > cap {cap:.3f}"
        )


@dataclass
class _ShotSpend:
    """Mutable per-shot accumulator of *committed-to-launch* cost (one fan-out)."""

    video_seconds: float = 0.0
    usd: float = 0.0
    reservations: list[BudgetReservation] = field(default_factory=list)

    def cost_in(self, unit: CostUnit) -> float:
        return self.usd if unit is CostUnit.USD else self.video_seconds


class MultiRenderBudgetGuard:
    """Gate + per-shot accountant for one best-of-N fan-out.

    One guard instance scopes one shot's fan-out: :meth:`decide` answers "may this shot
    fan out at all", :meth:`try_reserve` earmarks one candidate's seconds (refusing if
    the per-shot cap would break), and :meth:`commit_winner` / :meth:`release` settle
    the ledger when the run resolves. All async budget calls are funneled through the
    injected :class:`MultiRenderBudget`.
    """

    def __init__(
        self,
        budget: MultiRenderBudget,
        config: EnsembleConfig,
        *,
        book_id: str | None = None,
        session_id: str | None = None,
        scene_id: str | None = None,
    ) -> None:
        self._budget = budget
        self._config = config
        self._book_id = book_id
        self._session_id = session_id
        self._scene_id = scene_id
        self._spend = _ShotSpend()

    # -- the fan-out gate ------------------------------------------------- #

    def decide(self, spec: ShotRenderSpec) -> FanOutDecision:
        """May this shot fan out across multiple providers? (fail-closed cascade)."""
        cfg = self._config
        if not cfg.enabled:
            return FanOutDecision(False, FanOutRefusal.DISABLED)
        if spec.tier not in cfg.enabled_tiers:
            return FanOutDecision(False, FanOutRefusal.TIER_NOT_ENABLED)
        if cfg.max_candidates <= 1:
            return FanOutDecision(False, FanOutRefusal.SINGLE_CANDIDATE)
        if not self._budget.can_render_live():
            return FanOutDecision(False, FanOutRefusal.LIVE_GATE_OFF)
        return FanOutDecision(True, FanOutRefusal.ALLOWED)

    def can_render_live(self) -> bool:
        """Pass-through to the underlying live-video go-live gate."""
        return self._budget.can_render_live()

    # -- per-candidate reservation (cost-cap enforced at launch) ---------- #

    def _candidate_cost(self, spec: ShotRenderSpec, choice: ProviderChoice) -> tuple[float, float]:
        """(video_seconds, usd) this candidate would cost."""
        seconds = max(0.0, spec.duration_s) * choice.cost_per_s
        usd = max(0.0, spec.duration_s) * choice.usd_per_s
        return seconds, usd

    def would_exceed_cap(self, spec: ShotRenderSpec, choice: ProviderChoice) -> bool:
        """True when launching ``choice`` would push per-shot spend over the cap."""
        cap = self._config.per_shot_cost_cap
        if cap <= 0:
            return False
        seconds, usd = self._candidate_cost(spec, choice)
        unit = self._config.cost_unit
        prospective = self._spend.cost_in(unit) + (usd if unit is CostUnit.USD else seconds)
        return prospective > cap + 1e-12

    async def try_reserve(self, spec: ShotRenderSpec, choice: ProviderChoice) -> BudgetReservation:
        """Earmark this candidate's seconds; raise if the per-shot cap would break.

        Charges the prospective cost to the per-shot accumulator and reserves the
        seconds against the underlying ledger (which may itself raise if a *global*
        cap is hit). On any refusal nothing is left earmarked for this candidate.
        """
        if self.would_exceed_cap(spec, choice):
            seconds, usd = self._candidate_cost(spec, choice)
            unit = self._config.cost_unit
            raise CostCapExceeded(
                requested=usd if unit is CostUnit.USD else seconds,
                committed=self._spend.cost_in(unit),
                cap=self._config.per_shot_cost_cap,
                unit=unit,
            )
        seconds, usd = self._candidate_cost(spec, choice)
        reservation = await self._budget.reserve(
            seconds,
            book_id=self._book_id,
            session_id=self._session_id,
            scene_id=self._scene_id,
        )
        self._spend.video_seconds += seconds
        self._spend.usd += usd
        self._spend.reservations.append(reservation)
        return reservation

    # -- settlement ------------------------------------------------------- #

    async def commit_winner(
        self, reservation: BudgetReservation, *, actual_seconds: float | None = None
    ) -> None:
        """Charge the winning candidate's reservation against the ledger."""
        await self._budget.commit(reservation, actual_seconds=actual_seconds)

    async def release(self, reservation: BudgetReservation) -> None:
        """Return one losing/cancelled candidate's earmark to the ledger."""
        await self._budget.release(reservation)

    @property
    def reserved_video_seconds(self) -> float:
        """Total seconds earmarked across this shot's launched candidates."""
        return self._spend.video_seconds

    @property
    def reserved_usd(self) -> float:
        """Total USD earmarked across this shot's launched candidates."""
        return self._spend.usd


__all__ = [
    "CostCapExceeded",
    "FanOutDecision",
    "FanOutRefusal",
    "MultiRenderBudgetGuard",
]
