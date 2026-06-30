"""``BestOfNRenderer`` — render one shot on K models, score, pick the winner (§9.5/§11).

This is the orchestrator. Given a shot and a set of :class:`ProviderChoice` s, it:

#. asks the :class:`MultiRenderBudgetGuard` whether the shot may fan out at all
   (disabled-by-default, tier-gated, live-gated). If not, it degrades to a **single**
   best-priority render — never an accidental K× spend;
#. launches up to ``max_candidates`` providers, never more than ``max_concurrency`` at
   once, in a fixed deterministic order (priority, then name), each reserving its
   video-seconds through the guard (which refuses any launch that would breach the
   per-shot cost cap);
#. scores each completed clip with the :class:`QualityScorer`;
#. EARLY-STOPS: the instant a scored candidate clears ``good_enough_quality``, it stops
   launching new providers and cancels the ones still in flight (their reservations are
   released — losers cost nothing);
#. selects the winner deterministically under the configured objective (max-quality /
   quality-per-cost / quality-under-cost-cap / consistency-vote), commits the winner's
   reservation and releases every loser's;
#. emits a :class:`SelectionReport` explaining the whole decision.

Concurrency is bounded by a producer loop that holds at most ``max_concurrency``
candidates in flight and never launches a provider once early-stop fires; cancellation
is cooperative (losing tasks are cancelled and awaited so their reservations are always
released). The component is deterministic given deterministic providers/scorers:
ordering, the launch schedule, and the winner are all fixed — no RNG, no wall-clock.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Sequence
from dataclasses import dataclass

from app.core.logging import get_logger

from . import objectives
from .budget_guard import CostCapExceeded, FanOutDecision, MultiRenderBudgetGuard
from .models import (
    BudgetReservation,
    Candidate,
    CandidateStatus,
    EnsembleConfig,
    ProviderChoice,
    QualityScore,
    RenderOutput,
    SelectionReport,
    ShotRenderSpec,
)
from .protocols import EnsembleProvider, MultiRenderBudget, QualityScorer

logger = get_logger("app.video.ensemble.renderer")


class _RenderFailedError(Exception):
    """Internal: a provider's render raised (a losing candidate, not a crash)."""

    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(detail)


class _ScoreFailedError(Exception):
    """Internal: scoring a rendered clip raised."""

    def __init__(self, detail: str, *, output: RenderOutput) -> None:
        self.detail = detail
        self.output = output
        super().__init__(detail)


@dataclass(slots=True)
class _Outcome:
    """One candidate's result + the reservation still owed settlement (if any).

    A SCORED candidate carries a *held* reservation that :meth:`_finalize` commits (if
    it wins) or releases (if it loses). FAILED / SCORE_FAILED / CANCELLED / SKIPPED
    outcomes have already released (or never took) their reservation, so ``reservation``
    is ``None`` and there is nothing left to settle.
    """

    candidate: Candidate
    reservation: BudgetReservation | None = None
    #: The video-seconds / usd the held reservation would charge if this wins.
    video_seconds: float = 0.0
    usd: float = 0.0


class BestOfNRenderer:
    """Render-the-same-shot-on-K-models, score, and select the best (bounded, gated).

    Construction wires the collaborators (a provider registry keyed by name, the
    quality scorer, and the scarce-seconds budget); :meth:`render` runs one shot's
    fan-out + selection and returns a :class:`SelectionReport`. The renderer holds no
    per-shot mutable state — each :meth:`render` call is self-contained — so one
    instance safely serves many shots concurrently.
    """

    def __init__(
        self,
        providers: dict[str, EnsembleProvider],
        scorer: QualityScorer,
        budget: MultiRenderBudget,
        config: EnsembleConfig,
        *,
        book_id: str | None = None,
        session_id: str | None = None,
        scene_id: str | None = None,
    ) -> None:
        if not providers:
            raise ValueError("BestOfNRenderer requires at least one provider")
        self._providers = dict(providers)
        self._scorer = scorer
        self._budget = budget
        self._config = config
        self._book_id = book_id
        self._session_id = session_id
        self._scene_id = scene_id

    # ------------------------------------------------------------------ #
    # Public entry point
    # ------------------------------------------------------------------ #

    async def render(
        self, spec: ShotRenderSpec, choices: Sequence[ProviderChoice]
    ) -> SelectionReport:
        """Run best-of-N for ``spec`` over ``choices`` and return the selection report."""
        ordered = self._resolve_order(choices)
        guard = MultiRenderBudgetGuard(
            self._budget,
            self._config,
            book_id=self._book_id,
            session_id=self._session_id,
            scene_id=self._scene_id,
        )
        decision = guard.decide(spec)
        if not decision.allowed:
            return await self._render_single(spec, ordered, guard, decision)
        return await self._render_fanout(spec, ordered, guard)

    # ------------------------------------------------------------------ #
    # Ordering
    # ------------------------------------------------------------------ #

    def _resolve_order(self, choices: Sequence[ProviderChoice]) -> list[ProviderChoice]:
        """Deterministic launch order: by priority, then provider name.

        Unknown provider names (no registered backend) are dropped up front — they
        cannot render — keeping the fan-out honest about what it can actually launch.
        """
        known = [c for c in choices if c.name in self._providers]
        return sorted(known, key=lambda c: (c.priority, c.name))

    # ------------------------------------------------------------------ #
    # Degraded single-render path (fan-out refused)
    # ------------------------------------------------------------------ #

    async def _render_single(
        self,
        spec: ShotRenderSpec,
        ordered: Sequence[ProviderChoice],
        guard: MultiRenderBudgetGuard,
        decision: FanOutDecision,
    ) -> SelectionReport:
        """Render only the single best-priority provider (no fan-out, no K× spend)."""
        if not ordered:
            return self._empty_report(spec, fanned_out=False, reason="no eligible providers")
        outcomes = [await self._run_candidate(spec, ordered[0], order=0, guard=guard)]
        outcomes.extend(
            _Outcome(self._skipped(c, order=i, detail=f"fan-out {decision.reason}"))
            for i, c in enumerate(ordered[1:], start=1)
        )
        return await self._finalize(spec, outcomes, guard, early_stopped=False, fanned_out=False)

    # ------------------------------------------------------------------ #
    # Fan-out path
    # ------------------------------------------------------------------ #

    async def _render_fanout(
        self,
        spec: ShotRenderSpec,
        ordered: Sequence[ProviderChoice],
        guard: MultiRenderBudgetGuard,
    ) -> SelectionReport:
        """Launch up to N candidates (bounded), score, early-stop, then select.

        A producer loop launches candidates in deterministic order, holding at most
        ``max_concurrency`` in flight; it stops launching the instant early-stop fires,
        so a not-yet-launched provider is *never started* (deterministic suppression).
        In-flight losers are then cancelled. Providers past ``max_candidates`` are
        recorded as skipped without ever being considered.
        """
        pool = list(ordered[: self._config.max_candidates])
        skipped_tail = [
            _Outcome(self._skipped(c, order=i, detail="beyond max_candidates"))
            for i, c in enumerate(ordered[self._config.max_candidates :], start=len(pool))
        ]
        if not pool:
            return self._empty_report(spec, fanned_out=True, reason="no eligible providers")

        outcomes: dict[int, _Outcome] = {}
        early_stopped = await self._drive_pool(spec, pool, guard, outcomes)

        # Any provider in the pool that was never launched (suppressed by early-stop).
        for order, choice in enumerate(pool):
            outcomes.setdefault(
                order,
                _Outcome(self._skipped(choice, order=order, detail="early-stopped before launch")),
            )

        collected = [outcomes[i] for i in sorted(outcomes)]
        collected.extend(skipped_tail)
        return await self._finalize(
            spec, collected, guard, early_stopped=early_stopped, fanned_out=True
        )

    async def _drive_pool(
        self,
        spec: ShotRenderSpec,
        pool: Sequence[ProviderChoice],
        guard: MultiRenderBudgetGuard,
        outcomes: dict[int, _Outcome],
    ) -> bool:
        """Bounded producer/consumer: launch ≤ concurrency at a time; early-stop & cancel.

        Returns whether early-stop fired. Launching is suspended the moment a
        good-enough candidate lands, so providers not yet started stay unlaunched; the
        in-flight remainder are cancelled (and released) by :meth:`_cancel_pending`.
        """
        limit = max(1, self._config.max_concurrency)
        threshold = self._config.good_enough_quality
        next_order = 0
        early_stopped = False
        running: dict[asyncio.Task[_Outcome], tuple[int, ProviderChoice]] = {}
        try:
            while not early_stopped and (next_order < len(pool) or running):
                # Fill the in-flight set up to the concurrency limit.
                while not early_stopped and next_order < len(pool) and len(running) < limit:
                    choice = pool[next_order]
                    task = asyncio.ensure_future(
                        self._run_candidate(spec, choice, order=next_order, guard=guard)
                    )
                    running[task] = (next_order, choice)
                    next_order += 1
                if not running:
                    break
                done, _ = await asyncio.wait(running, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    order, _choice = running.pop(task)
                    outcome = task.result()
                    outcomes[order] = outcome
                    if objectives.is_good_enough(outcome.candidate, threshold):
                        early_stopped = True
        finally:
            await self._cancel_pending(running, outcomes)
        return early_stopped

    async def _cancel_pending(
        self,
        running: dict[asyncio.Task[_Outcome], tuple[int, ProviderChoice]],
        outcomes: dict[int, _Outcome],
    ) -> None:
        """Cancel still-running candidate tasks and record them as CANCELLED.

        Each cancelled task's :meth:`_run_candidate` releases its reservation in its
        ``except CancelledError`` path, so a cancelled loser costs nothing. A task that
        had already finished (its result not yet harvested) is recorded as its real
        outcome rather than CANCELLED.
        """
        for task in running:
            task.cancel()
        for task, (order, choice) in running.items():
            with contextlib.suppress(asyncio.CancelledError):
                outcome = await task
                outcomes.setdefault(order, outcome)
            outcomes.setdefault(
                order,
                _Outcome(
                    Candidate(
                        provider=choice.name,
                        status=CandidateStatus.CANCELLED,
                        order=order,
                        detail="cancelled by early-stop",
                    )
                ),
            )

    # ------------------------------------------------------------------ #
    # Per-candidate execution
    # ------------------------------------------------------------------ #

    async def _run_candidate(
        self,
        spec: ShotRenderSpec,
        choice: ProviderChoice,
        *,
        order: int,
        guard: MultiRenderBudgetGuard,
    ) -> _Outcome:
        """Reserve → render → score one candidate; release the reservation on any failure.

        On success the reservation is *held* on the returned :class:`_Outcome` for
        :meth:`_finalize` to settle. On render/score failure or cancellation the
        reservation is released here so a non-winning candidate never lingers.
        """
        try:
            reservation = await guard.try_reserve(spec, choice)
        except CostCapExceeded as exc:
            return _Outcome(self._skipped(choice, order=order, detail=str(exc), over_cap=True))

        video_seconds = reservation.video_seconds
        usd = max(0.0, spec.duration_s) * choice.usd_per_s
        try:
            output = await self._render_one(choice, spec)
            score = await self._score_one(output, spec)
        except asyncio.CancelledError:
            await self._safe_release(guard, reservation)
            raise
        except _RenderFailedError as exc:
            await self._safe_release(guard, reservation)
            return _Outcome(
                Candidate(
                    provider=choice.name,
                    status=CandidateStatus.FAILED,
                    order=order,
                    detail=exc.detail,
                )
            )
        except _ScoreFailedError as exc:
            await self._safe_release(guard, reservation)
            return _Outcome(
                Candidate(
                    provider=choice.name,
                    status=CandidateStatus.SCORE_FAILED,
                    order=order,
                    output=exc.output,
                    detail=exc.detail,
                )
            )
        candidate = Candidate(
            provider=choice.name,
            status=CandidateStatus.SCORED,
            order=order,
            output=output,
            score=score,
            video_seconds=video_seconds,
            usd=usd,
        )
        return _Outcome(candidate, reservation=reservation, video_seconds=video_seconds, usd=usd)

    async def _render_one(self, choice: ProviderChoice, spec: ShotRenderSpec) -> RenderOutput:
        provider = self._providers[choice.name]
        try:
            return await provider.render(spec)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # provider raised — a losing candidate, not a crash
            raise _RenderFailedError(f"{type(exc).__name__}: {exc}") from exc

    async def _score_one(self, output: RenderOutput, spec: ShotRenderSpec) -> QualityScore:
        try:
            return await self._scorer.score(output, spec)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # scorer raised — candidate can't be ranked
            raise _ScoreFailedError(f"{type(exc).__name__}: {exc}", output=output) from exc

    # ------------------------------------------------------------------ #
    # Selection + settlement + report
    # ------------------------------------------------------------------ #

    async def _finalize(
        self,
        spec: ShotRenderSpec,
        outcomes: list[_Outcome],
        guard: MultiRenderBudgetGuard,
        *,
        early_stopped: bool,
        fanned_out: bool,
    ) -> SelectionReport:
        """Pick the winner, settle the ledger (commit winner / release losers), report."""
        candidates = [o.candidate for o in outcomes]
        winner = objectives.select_winner(candidates, self._config)
        charged_s = 0.0
        charged_usd = 0.0
        for outcome in outcomes:
            if outcome.reservation is None:
                continue  # already settled (failed/cancelled/skipped)
            if winner is not None and outcome.candidate is winner:
                await self._safe_commit(guard, outcome.reservation)
                charged_s = outcome.video_seconds
                charged_usd = outcome.usd
            else:
                await self._safe_release(guard, outcome.reservation)

        reason = (
            objectives.explain_winner(winner, candidates, self._config)
            if winner is not None
            else "no eligible candidate"
        )
        report = SelectionReport(
            shot_id=spec.shot_id,
            objective=self._config.objective,
            cost_unit=self._config.cost_unit,
            enabled=self._config.enabled,
            winner=winner.provider if winner is not None else None,
            candidates=candidates,
            early_stopped=early_stopped,
            fanned_out=fanned_out,
            winning_score=(winner.score.composite if winner and winner.score else None),
            charged_video_seconds=charged_s,
            charged_usd=charged_usd,
            reason=reason,
        )
        logger.info("ensemble.selection", **report.as_log_fields())
        return report

    # ------------------------------------------------------------------ #
    # Small helpers
    # ------------------------------------------------------------------ #

    @staticmethod
    async def _safe_release(guard: MultiRenderBudgetGuard, reservation: BudgetReservation) -> None:
        with contextlib.suppress(Exception):
            await guard.release(reservation)

    @staticmethod
    async def _safe_commit(guard: MultiRenderBudgetGuard, reservation: BudgetReservation) -> None:
        await guard.commit_winner(reservation)

    @staticmethod
    def _skipped(
        choice: ProviderChoice,
        *,
        order: int,
        detail: str,
        over_cap: bool = False,
    ) -> Candidate:
        return Candidate(
            provider=choice.name,
            status=CandidateStatus.OVER_CAP if over_cap else CandidateStatus.SKIPPED,
            order=order,
            detail=detail,
        )

    def _empty_report(
        self, spec: ShotRenderSpec, *, fanned_out: bool, reason: str
    ) -> SelectionReport:
        return SelectionReport(
            shot_id=spec.shot_id,
            objective=self._config.objective,
            cost_unit=self._config.cost_unit,
            enabled=self._config.enabled,
            winner=None,
            candidates=[],
            early_stopped=False,
            fanned_out=fanned_out,
            winning_score=None,
            reason=reason,
        )


__all__ = [
    "BestOfNRenderer",
]
