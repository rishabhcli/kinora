"""The end-to-end render reliability coordinator.

``ReliableRenderCoordinator.render(shot)`` is the single high-level entry point:
*render this shot reliably, across any available providers, honoring budget + SLA
+ quality.* It orchestrates the round-1/round-2 primitives (router failover/hedge,
capacity/SLA governor, cost budget, quality gate, async jobs) behind the local
Protocols in :mod:`.protocols`, with deterministic time from :mod:`.clock`.

The loop (one shot):

1. **Plan** — :func:`build_candidates` ranks the admissible providers (governor
   admission + budget pre-flight + reputation/load/cost weighting).
2. **Attempt, in rank order** — for each candidate, with bounded coordinator-level
   retries:
   a. check the per-shot **deadline**; on expiry stop and ship best-so-far;
   b. **reserve** the cost; if the budget denies it, abort the shot cleanly
      (release nothing — nothing was held) and fall back;
   c. ask the **router** to render (its own retries/hedge run inside); on a
      provider error, release the reservation and move to the next provider;
   d. run the **quality gate**; below ``shot.min_quality`` → reject, release the
      reservation, remember it as best-so-far if it beats what we have, and
      **escalate** to the next-best provider rather than shipping garbage;
   e. on pass → settle the reservation (charge actual cost) and ship.
3. **Graceful fallback** — if no candidate shipped a passing full result, return
   the best-so-far accepted-tier artifact, else synthesize a degraded-but-real
   narrated-text card. The coordinator **never** returns nothing.

Every step appends an :class:`AttemptRecord` to a :class:`RenderAttemptLog`, so the
returned :class:`RenderOutcome` is fully self-describing for observability.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping

from app.core.logging import get_logger
from app.video.reliability.candidates import Candidate, CandidatePlan, build_candidates
from app.video.reliability.clock import Clock, Sleep
from app.video.reliability.config import ReliabilityConfig
from app.video.reliability.models import (
    AttemptRecord,
    AttemptStatus,
    FallbackReason,
    RenderAttemptLog,
    RenderOutcome,
    RenderResult,
    RenderTier,
    ShotSpec,
)
from app.video.reliability.protocols import (
    BudgetReservation,
    CostBudgetProtocol,
    GovernorProtocol,
    JobSinkProtocol,
    QualityGateProtocol,
    QualityReputationProtocol,
    RouterProtocol,
)

logger = get_logger("app.video.reliability.coordinator")


class _NullJobSink:
    """A no-op :class:`JobSinkProtocol` used when the caller wires none."""

    def started(self, shot: ShotSpec) -> None:  # noqa: D401 - trivial
        return None

    def progress(self, shot: ShotSpec, event: str, fields: Mapping[str, object]) -> None:
        return None

    def finished(self, shot: ShotSpec, outcome_ok: bool, tier: int) -> None:
        return None


class ReliableRenderCoordinator:
    """Coordinate a reliable, budget/SLA/quality-honoring render of one shot."""

    def __init__(
        self,
        *,
        router: RouterProtocol,
        governor: GovernorProtocol,
        reputation: QualityReputationProtocol,
        quality_gate: QualityGateProtocol,
        budget: CostBudgetProtocol,
        config: ReliabilityConfig | None = None,
        jobs: JobSinkProtocol | None = None,
        clock: Clock = time.monotonic,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._router = router
        self._governor = governor
        self._reputation = reputation
        self._gate = quality_gate
        self._budget = budget
        self._config = config or ReliabilityConfig()
        self._jobs: JobSinkProtocol = jobs or _NullJobSink()
        self._clock = clock
        self._sleep = sleep

    async def render(self, shot: ShotSpec) -> RenderOutcome:
        """Render ``shot`` reliably; always return a real artifact + a full log."""
        t0 = self._clock()
        deadline_at = t0 + shot.deadline_s
        log = RenderAttemptLog(shot_id=shot.shot_id, deadline_s=shot.deadline_s)
        self._jobs.started(shot)

        plan = build_candidates(
            shot,
            router=self._router,
            governor=self._governor,
            reputation=self._reputation,
            budget=self._budget,
            config=self._config,
        )
        log.ranked_providers = [c.provider for c in plan.ranked]
        self._record_pruned(plan, log, t0)

        if plan.is_empty:
            logger.info("render.no_candidates", shot_id=shot.shot_id)
            return self._finalize(shot, log, t0, None, FallbackReason.NO_CANDIDATES)

        best_so_far: RenderResult | None = None
        budget_exhausted = False
        deadline_hit = False
        saw_quality_reject = False

        for rank, cand in enumerate(plan.ranked):
            if self._clock() >= deadline_at:
                deadline_hit = True
                self._record_deadline(cand, rank, log, t0)
                break

            attempt_result, status = await self._try_provider(
                shot, cand, rank, deadline_at, log, t0
            )
            if status is AttemptStatus.ACCEPTED and attempt_result is not None:
                return self._finalize(shot, log, t0, attempt_result, FallbackReason.NONE)
            if status is AttemptStatus.BUDGET_DENIED:
                budget_exhausted = True
                break
            if status is AttemptStatus.DEADLINE_EXCEEDED:
                deadline_hit = True
                break
            if status is AttemptStatus.QUALITY_REJECTED:
                saw_quality_reject = True
                best_so_far = self._better(best_so_far, attempt_result)
            # PROVIDER_ERROR / GOVERNOR_BLOCKED -> just escalate to the next.

        reason = self._fallback_reason(
            budget_exhausted=budget_exhausted,
            deadline_hit=deadline_hit,
            saw_quality_reject=saw_quality_reject,
        )
        return self._finalize(shot, log, t0, best_so_far, reason)

    # ----- one provider, with bounded coordinator-level retries -----------------

    async def _try_provider(
        self,
        shot: ShotSpec,
        cand: Candidate,
        rank: int,
        deadline_at: float,
        log: RenderAttemptLog,
        t0: float,
    ) -> tuple[RenderResult | None, AttemptStatus]:
        """Attempt one provider; return its produced result (if any) + final status."""
        attempts = max(1, self._config.per_provider_attempts)
        last_status = AttemptStatus.PROVIDER_ERROR
        last_result: RenderResult | None = None

        for attempt_idx in range(attempts):
            if self._clock() >= deadline_at:
                self._record_deadline(cand, rank, log, t0)
                return None, AttemptStatus.DEADLINE_EXCEEDED

            started = self._clock()
            est = max(0.0, self._budget.estimate(cand.provider, shot))
            reservation = self._budget.reserve(cand.provider, est)
            if reservation is None:
                log.add(
                    self._record(
                        rank,
                        cand.provider,
                        AttemptStatus.BUDGET_DENIED,
                        started,
                        t0,
                        cost_reserved=0.0,
                        detail=f"budget denied reservation of ${est:.4f}",
                    )
                )
                return None, AttemptStatus.BUDGET_DENIED

            result, status, charged, detail = await self._render_and_gate(
                shot, cand, reservation
            )
            log.add(
                self._record(
                    rank,
                    cand.provider,
                    status,
                    started,
                    t0,
                    quality=(result.quality if result is not None else None),
                    cost_reserved=reservation.amount_usd,
                    cost_charged=charged,
                    attempts_used=1,
                    detail=detail,
                )
            )
            if status is AttemptStatus.ACCEPTED:
                return result, status

            last_status, last_result = status, result
            if status is AttemptStatus.QUALITY_REJECTED:
                # A bad clip won't get better on a plain retry of the same provider;
                # escalate to the next-best candidate instead.
                return last_result, status
            # PROVIDER_ERROR: retry this provider after a bounded backoff (if any
            # attempts remain), else escalate.
            if attempt_idx + 1 < attempts:
                await self._backoff(attempt_idx)

        return last_result, last_status

    async def _render_and_gate(
        self,
        shot: ShotSpec,
        cand: Candidate,
        reservation: BudgetReservation,
    ) -> tuple[RenderResult | None, AttemptStatus, float, str]:
        """Run the router render + quality gate for one reserved attempt.

        Returns ``(result, status, charged_usd, detail)``. On any non-accept path
        the reservation is released so a failed/rejected attempt never charges the
        budget.
        """
        try:
            result = await self._router.render(cand.provider, shot)
        except asyncio.CancelledError:
            reservation.release()
            raise
        except Exception as exc:  # noqa: BLE001 - any provider/router failure escalates
            reservation.release()
            return None, AttemptStatus.PROVIDER_ERROR, 0.0, _short_error(exc)

        try:
            score = await self._gate.score(shot, result)
        except asyncio.CancelledError:
            reservation.release()
            raise
        except Exception as exc:  # noqa: BLE001 - a gate that errors is treated as a reject
            reservation.release()
            scored = result.model_copy(update={"quality": 0.0})
            detail = f"quality gate raised: {_short_error(exc)}"
            return scored, AttemptStatus.QUALITY_REJECTED, 0.0, detail

        scored = result.model_copy(update={"quality": _clamp01(score)})
        if scored.quality < shot.min_quality:
            reservation.release()
            return (
                scored,
                AttemptStatus.QUALITY_REJECTED,
                0.0,
                f"score {scored.quality:.3f} < floor {shot.min_quality:.3f}",
            )

        charged = scored.cost_usd if scored.cost_usd > 0 else reservation.amount_usd
        reservation.settle(charged)
        return scored, AttemptStatus.ACCEPTED, charged, f"accepted at q={scored.quality:.3f}"

    # ----- fallback + finalization ---------------------------------------------

    def _finalize(
        self,
        shot: ShotSpec,
        log: RenderAttemptLog,
        t0: float,
        winner: RenderResult | None,
        reason: FallbackReason,
    ) -> RenderOutcome:
        """Stamp totals, pick the shipped artifact (never nothing), emit the log.

        A clean accept ships ``winner`` as-is under :attr:`FallbackReason.NONE`. Any
        other path is a degradation: a best-so-far clip that was *kept but rejected*
        by the quality gate ships flagged ``degraded=True`` (so a downstream consumer
        knows it is below the floor), and if there is no best-so-far at all we
        synthesize the bottom-rung narrated-text card.
        """
        final_reason = reason
        if winner is None:
            result = self._degraded_card(shot)
            if final_reason is FallbackReason.NONE:
                final_reason = FallbackReason.ALL_PROVIDERS_FAILED
        elif final_reason is FallbackReason.NONE:
            result = winner  # clean accept, shipped verbatim
        else:
            # Best-so-far under a fallback condition -> flag it as degraded.
            result = winner.model_copy(update={"degraded": True})

        log.total_elapsed_s = max(0.0, self._clock() - t0)
        log.total_cost_charged_usd = round(
            sum(rec.cost_charged_usd for rec in log.attempts), 10
        )
        log.fallback_reason = final_reason
        log.final_tier = result.tier
        log.final_status = (
            AttemptStatus.ACCEPTED
            if final_reason is FallbackReason.NONE
            else self._terminal_status(final_reason)
        )

        ok = True  # we always ship a real artifact
        self._jobs.finished(shot, ok, int(result.tier))
        logger.info(
            "render.done",
            shot_id=shot.shot_id,
            provider=result.provider,
            tier=int(result.tier),
            degraded=result.degraded,
            fallback_reason=final_reason.value,
            providers_tried=len(log.providers_tried),
            elapsed_s=round(log.total_elapsed_s, 4),
            charged_usd=log.total_cost_charged_usd,
        )
        return RenderOutcome(ok=ok, result=result, fallback_reason=final_reason, log=log)

    def _degraded_card(self, shot: ShotSpec) -> RenderResult:
        """Synthesize the bottom-rung narrated-text card — always available, $0."""
        return RenderResult(
            shot_id=shot.shot_id,
            provider="reliability.fallback",
            tier=RenderTier.NARRATED_TEXT,
            uri=f"kinora://fallback/{shot.shot_id}/narrated-text-card",
            quality=1.0,  # trivially valid at its own tier
            cost_usd=0.0,
            video_seconds=0.0,
            degraded=True,
        )

    # ----- logging helpers ------------------------------------------------------

    def _record_pruned(self, plan: CandidatePlan, log: RenderAttemptLog, t0: float) -> None:
        now = self._clock() - t0
        for pruned in plan.pruned:
            log.add(
                AttemptRecord(
                    rank=_PRUNED_RANK,
                    provider=pruned.provider,
                    status=pruned.status,
                    started_at_s=max(0.0, now),
                    ended_at_s=max(0.0, now),
                    attempts_used=0,
                    detail=pruned.detail,
                )
            )

    def _record_deadline(
        self, cand: Candidate, rank: int, log: RenderAttemptLog, t0: float
    ) -> None:
        now = max(0.0, self._clock() - t0)
        log.add(
            AttemptRecord(
                rank=rank,
                provider=cand.provider,
                status=AttemptStatus.DEADLINE_EXCEEDED,
                started_at_s=now,
                ended_at_s=now,
                attempts_used=0,
                detail="per-shot deadline elapsed before this provider could be tried",
            )
        )

    def _record(
        self,
        rank: int,
        provider: str,
        status: AttemptStatus,
        started_at_abs: float,
        t0: float,
        *,
        quality: float | None = None,
        cost_reserved: float = 0.0,
        cost_charged: float = 0.0,
        attempts_used: int = 1,
        detail: str = "",
    ) -> AttemptRecord:
        """Build a record with start/end stamped relative to the per-call ``t0``."""
        ended_abs = self._clock()
        return AttemptRecord(
            rank=rank,
            provider=provider,
            status=status,
            started_at_s=max(0.0, started_at_abs - t0),
            ended_at_s=max(0.0, ended_abs - t0),
            quality=quality,
            cost_reserved_usd=cost_reserved,
            cost_charged_usd=cost_charged,
            attempts_used=attempts_used,
            detail=detail,
        )

    # ----- small pure helpers ---------------------------------------------------

    async def _backoff(self, attempt_idx: int) -> None:
        base = self._config.retry_backoff_base_s
        delay = min(self._config.retry_backoff_max_s, base * (2**attempt_idx))
        if delay > 0:
            await self._sleep(delay)

    @staticmethod
    def _better(
        current: RenderResult | None, candidate: RenderResult | None
    ) -> RenderResult | None:
        if candidate is None:
            return current
        if current is None:
            return candidate
        # Prefer higher tier, then higher quality.
        if (candidate.tier, candidate.quality) > (current.tier, current.quality):
            return candidate
        return current

    @staticmethod
    def _fallback_reason(
        *, budget_exhausted: bool, deadline_hit: bool, saw_quality_reject: bool
    ) -> FallbackReason:
        if budget_exhausted:
            return FallbackReason.BUDGET_EXHAUSTED
        if deadline_hit:
            return FallbackReason.DEADLINE_EXCEEDED
        if saw_quality_reject:
            return FallbackReason.QUALITY_FLOOR
        return FallbackReason.ALL_PROVIDERS_FAILED

    @staticmethod
    def _terminal_status(reason: FallbackReason) -> AttemptStatus:
        return {
            FallbackReason.BUDGET_EXHAUSTED: AttemptStatus.BUDGET_DENIED,
            FallbackReason.DEADLINE_EXCEEDED: AttemptStatus.DEADLINE_EXCEEDED,
            FallbackReason.QUALITY_FLOOR: AttemptStatus.QUALITY_REJECTED,
            FallbackReason.NO_CANDIDATES: AttemptStatus.GOVERNOR_BLOCKED,
            FallbackReason.ALL_PROVIDERS_FAILED: AttemptStatus.PROVIDER_ERROR,
        }.get(reason, AttemptStatus.PROVIDER_ERROR)


#: Sentinel rank for pruned (never-attempted) providers in the attempt log.
_PRUNED_RANK = 9_999


def _clamp01(value: float) -> float:
    return 0.0 if value < 0.0 else 1.0 if value > 1.0 else value


def _short_error(exc: BaseException) -> str:
    msg = str(exc).strip() or exc.__class__.__name__
    return f"{exc.__class__.__name__}: {msg}"[:200]


__all__ = ["ReliableRenderCoordinator"]
