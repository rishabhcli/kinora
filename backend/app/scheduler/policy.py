"""Scheduler policy bundle (kinora.md §4.5/§4.6/§13).

A *policy* is the set of tunables that shape the Scheduler's behaviour: the §4.5
watermarks/horizons, whether adaptive watermarks (Phase 3) and the budget
optimiser (Phase 4) are on, and the fairness/adaptive gains. Bundling them into a
single immutable :class:`SchedulerPolicy` is what makes an offline A/B
(:mod:`app.scheduler.experiment`) possible: you replay the same reading traces
under policy A and policy B and compare the §13 buffer-health numbers.

A policy carries only knobs — no behaviour, no spend. Materialising it into a
``Settings`` for the simulation harness (:meth:`SchedulerPolicy.to_settings`)
produces a settings object whose watermark fields reflect the policy, leaving the
budget/live-gate fields untouched so the harness's zero-spend invariant holds: a
policy can change *what to buffer when*, never *whether the live gate is open*.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from app.core.config import Settings, get_settings


@dataclass(frozen=True, slots=True)
class SchedulerPolicy:
    """An immutable bundle of Scheduler tunables for evaluation (§4.5/§4.6).

    Defaults reproduce the inherited §4.5 constants with every adaptive feature
    OFF — so the "control" arm of any A/B is exactly today's behaviour.
    """

    name: str = "baseline"
    low_s: float = 25.0
    high_s: float = 75.0
    commit_horizon_s: float = 45.0
    spec_horizon_s: float = 240.0
    keyframe_cap: int = 12
    #: Phase 3 — tune watermarks to the reader's variance.
    adaptive_watermarks: bool = False
    #: Phase 4 — knapsack the affordable candidates near the budget floor.
    budget_optimizer: bool = False
    #: Phase 5 — fair-share a shared pool across sessions (multi-reader runs).
    fairness: bool = False

    @classmethod
    def from_settings(
        cls, settings: Settings | None = None, *, name: str = "baseline"
    ) -> SchedulerPolicy:
        """A policy seeded from the live §4.5 settings constants."""
        s = settings or get_settings()
        return cls(
            name=name,
            low_s=s.watermark_low_s,
            high_s=s.watermark_high_s,
            commit_horizon_s=s.commit_horizon_s,
            spec_horizon_s=s.spec_horizon_s,
        )

    def with_(self, **changes: object) -> SchedulerPolicy:
        """A copy with overrides (e.g. ``policy.with_(name='deep', high_s=90)``)."""
        return replace(self, **changes)  # type: ignore[arg-type]

    def to_settings(self, base: Settings | None = None) -> Settings:
        """Materialise the watermark knobs onto a copy of ``base`` settings.

        Only the four watermark/horizon fields are overridden; budget, live-gate,
        and concurrency settings are copied verbatim, so the harness's zero-spend
        invariant and ``can_render_live()`` gating are unaffected.
        """
        base = base or get_settings()
        return base.model_copy(
            update={
                "watermark_low_s": self.low_s,
                "watermark_high_s": self.high_s,
                "commit_horizon_s": self.commit_horizon_s,
                "spec_horizon_s": self.spec_horizon_s,
            }
        )


__all__ = ["SchedulerPolicy"]
