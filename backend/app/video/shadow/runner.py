"""Shadow mode — fork a candidate render off the critical path, never blocking it.

The single most important invariant of this whole subsystem lives here:

    **The shadow render NEVER affects what the reader sees.**

So the runner is built so it *cannot* — by construction, not by discipline:

* It is handed the production :class:`RenderOutcome` (what the reader saw / would
  see). It never produces that result and never mutates it; it returns it
  unchanged, every code path, including when the candidate render explodes.
* The candidate render is wrapped so any exception is swallowed into a typed
  :class:`FailureKind.PROVIDER_ERROR` outcome — a buggy candidate provider can
  never propagate into the reader's request.
* Candidate spend is reserved against the *separate, zero-by-default*
  :class:`EvalBudget`. Unfunded ⇒ the candidate is never even rendered (recorded
  as :class:`FailureKind.GATED`), so turning shadow mode on cannot spend a real
  video-second or touch the reader budget.
* The candidate render is launched only when the :class:`Sampler` includes the
  shot, so eval load is a bounded fraction of traffic.

:meth:`ShadowRunner.observe` is the production entrypoint: the orchestrator calls
it *after* it already has the reader's result, passing that result in. It returns a
:class:`ShadowObservation` carrying the (untouched) production outcome plus an
optional :class:`PairedSample` to feed the collector. Because the production result
is an input, the runner is trivially "off the critical path" — the reader has
already been served by the time it runs.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.logging import get_logger

from .budget import EvalBudget, EvalBudgetExhausted
from .clock import MonotonicClock
from .collector import ComparisonDataset, PairedSample
from .seams import (
    Clock,
    FailureKind,
    QualityScorer,
    RenderOutcome,
    Sampler,
    ShotSpec,
    VideoRenderProvider,
)

logger = get_logger("app.video.shadow.runner")


@dataclass(frozen=True, slots=True)
class ShadowObservation:
    """The outcome of one :meth:`ShadowRunner.observe` call.

    ``production`` is the reader's result, returned **unchanged** — callers can rely
    on ``observation.production is the_input_outcome``. ``sample`` is present iff the
    shot was sampled (a paired record was produced); ``None`` otherwise.
    ``sampled`` distinguishes "not sampled" from "sampled but candidate failed".
    """

    production: RenderOutcome
    sampled: bool
    sample: PairedSample | None


class ShadowRunner:
    """Forks a candidate render for a sampled fraction of real renders.

    All collaborators are injected (provider, scorer, sampler, clock, budget) so the
    runner is fully fake-able with no infra. The runner is stateless beyond its
    collaborators; the accumulating dataset lives in the collector the caller owns.
    """

    def __init__(
        self,
        *,
        candidate: VideoRenderProvider,
        scorer: QualityScorer,
        sampler: Sampler,
        eval_budget: EvalBudget,
        clock: Clock | None = None,
    ) -> None:
        self._candidate = candidate
        self._scorer = scorer
        self._sampler = sampler
        self._budget = eval_budget
        self._clock = clock or MonotonicClock()

    async def observe(
        self,
        spec: ShotSpec,
        production: RenderOutcome,
    ) -> ShadowObservation:
        """Maybe shadow-render ``spec`` on the candidate; return a paired record.

        ``production`` is the reader's already-computed outcome. It is returned
        untouched no matter what happens to the candidate render. The candidate is
        rendered only if (a) the sampler includes ``spec.shot_id`` and (b) the eval
        budget can fund its expected video-seconds.
        """
        if not self._sampler.in_sample(spec.shot_id):
            return ShadowObservation(production=production, sampled=False, sample=None)

        candidate_outcome = await self._render_candidate(spec)
        sample = await self._pair(spec, production, candidate_outcome)
        return ShadowObservation(production=production, sampled=True, sample=sample)

    async def observe_into(
        self,
        spec: ShotSpec,
        production: RenderOutcome,
        dataset: ComparisonDataset,
    ) -> ShadowObservation:
        """:meth:`observe` and, if a paired sample resulted, add it to ``dataset``."""
        observation = await self.observe(spec, production)
        if observation.sample is not None:
            dataset.add(observation.sample)
        return observation

    # ----------------------------------------------------------------- #

    async def _render_candidate(self, spec: ShotSpec) -> RenderOutcome:
        """Render the candidate, metering spend + latency, never raising.

        Reserves the shot's expected video-seconds against the eval budget first;
        an unfunded/exhausted budget short-circuits to a GATED outcome with no
        provider call. Any provider exception becomes a PROVIDER_ERROR outcome so
        the reader path is insulated.
        """
        try:
            reservation = self._budget.reserve(spec.shot_id, spec.expected_video_seconds)
        except EvalBudgetExhausted:
            logger.debug(
                "shadow.candidate.gated_budget",
                shot_id=spec.shot_id,
                candidate=self._candidate.model_id,
            )
            return RenderOutcome(
                model=self._candidate.model_id,
                succeeded=False,
                failure=FailureKind.GATED,
                video_seconds=0.0,
            )

        start = self._clock.monotonic()
        try:
            outcome = await self._candidate.render(spec)
        except Exception:  # noqa: BLE001 - a candidate fault must never reach the reader
            self._budget.release(reservation)
            logger.warning(
                "shadow.candidate.render_raised",
                shot_id=spec.shot_id,
                candidate=self._candidate.model_id,
                exc_info=True,
            )
            return RenderOutcome(
                model=self._candidate.model_id,
                succeeded=False,
                failure=FailureKind.PROVIDER_ERROR,
                video_seconds=0.0,
            )

        # A candidate that deliberately gated bills nothing; release the hold.
        if outcome.is_gated:
            self._budget.release(reservation)
        else:
            self._budget.settle(reservation, outcome.video_seconds)

        # Stamp a measured latency if the provider didn't supply one.
        if outcome.latency_ms <= 0.0:
            elapsed_ms = max(0.0, (self._clock.monotonic() - start) * 1000.0)
            outcome = outcome.model_copy(update={"latency_ms": elapsed_ms})
        return outcome

    async def _pair(
        self,
        spec: ShotSpec,
        production: RenderOutcome,
        candidate: RenderOutcome,
    ) -> PairedSample:
        """Score both sides where needed and build the paired record.

        Only successful, not-yet-scored renders are sent to the scorer (a failed or
        gated render has no clip to score). The production outcome is never mutated
        in place — if it needs a score we copy it.
        """
        scored_candidate = await self._maybe_score(spec, candidate)
        scored_production = await self._maybe_score(spec, production)
        return PairedSample(
            shot_id=spec.shot_id,
            production=scored_production,
            candidate=scored_candidate,
            spec=spec,
        )

    async def _maybe_score(self, spec: ShotSpec, outcome: RenderOutcome) -> RenderOutcome:
        if not outcome.succeeded or outcome.quality is not None:
            return outcome
        try:
            quality = await self._scorer.score(spec, outcome)
        except Exception:  # noqa: BLE001 - a scorer fault must not abort the comparison
            logger.warning(
                "shadow.scorer.raised",
                shot_id=spec.shot_id,
                model=outcome.model,
                exc_info=True,
            )
            return outcome
        clamped = min(1.0, max(0.0, float(quality)))
        return outcome.model_copy(update={"quality": clamped})


__all__ = ["ShadowObservation", "ShadowRunner"]
