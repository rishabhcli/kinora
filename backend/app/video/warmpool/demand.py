"""Predicted near-term demand → cost-aware warm target (pure logic).

The pre-warm scheduler keeps "just enough" sessions warm: enough to hide
cold-start latency on the next burst of renders, not so many that idle
connections waste resources. That tradeoff is computed here, from three inputs:

* a **demand signal** — how many renders this provider is expected to start in
  the near-term horizon. The render path / scheduler feeds it through
  :meth:`DemandModel.observe` (a render was just dispatched) or
  :meth:`DemandModel.set_hint` (the scheduler's own look-ahead, driven by reader
  velocity and the buffer watermark — the seam to §5.3/§4.3). Either way it lands
  as an EWMA *rate* (renders/second);
* the **cold-start savings** for the provider (from :mod:`app.video.warmpool.cost`)
  — if opening cold is already as fast as warm, the warm floor collapses to zero;
* the **pool bounds** (``min_warm`` / ``max_warm`` from
  :class:`~app.video.warmpool.settings.WarmPoolConfig`).

The target is ``ceil(rate × horizon)``, clamped into ``[floor, max_warm]`` where
``floor`` is ``min_warm`` for a worth-warming provider and ``0`` otherwise. This
is the classic *little's-law-ish* "concurrent arrivals over the time it takes to
become useful" sizing, but bounded so a velocity spike can't provoke unbounded
idle sessions. Pure: the model owns the math, the pool owns the clock and the bounds.
"""

from __future__ import annotations

import math

from pydantic import BaseModel

#: EWMA smoothing for the observed demand rate. Lower = steadier (slower to chase
#: a single burst); chosen so a sustained spike lifts the target within a few ticks.
DEFAULT_DEMAND_ALPHA = 0.4
#: Decay applied to the demand rate on a tick with *no* new demand, so the target
#: relaxes back toward the floor after a burst clears (cost-aware: don't hold warm
#: sessions for demand that has gone away).
DEFAULT_DEMAND_DECAY = 0.6


class DemandModel(BaseModel):
    """Per-provider near-term demand estimate feeding the warm target (pure).

    Holds an EWMA ``rate_per_s`` of render dispatches plus the latest external
    ``hint_per_s`` from the scheduler. The effective rate is the *max* of the two:
    a scheduler look-ahead can pre-warm *before* the first render arrives (the
    whole point of generating ahead of the reader), while observed demand keeps the
    target up if the hint is stale or absent.
    """

    provider: str
    rate_per_s: float = 0.0
    hint_per_s: float = 0.0
    alpha: float = DEFAULT_DEMAND_ALPHA
    decay: float = DEFAULT_DEMAND_DECAY
    _last_observe_seq: int = 0

    def observe(self, renders: int, *, window_s: float) -> None:
        """Fold in ``renders`` dispatched over the last ``window_s`` seconds."""
        if window_s <= 0:
            return
        instant = max(0, int(renders)) / float(window_s)
        self.rate_per_s = (1.0 - self.alpha) * self.rate_per_s + self.alpha * instant

    def decay_idle(self) -> None:
        """Relax the observed rate on a tick with no new demand (toward the floor)."""
        self.rate_per_s *= self.decay
        if self.rate_per_s < 1e-6:
            self.rate_per_s = 0.0

    def set_hint(self, renders_per_s: float) -> None:
        """Set the scheduler's near-term look-ahead rate (the predictive seam).

        This is where reader velocity / buffer-watermark pressure enters: the
        scheduler knows it is about to need *N* shots in the next few seconds and
        hints that rate so the pool warms sessions *ahead* of the demand.
        """
        self.hint_per_s = max(0.0, float(renders_per_s))

    @property
    def effective_rate_per_s(self) -> float:
        """The rate the warm target is sized against (max of observed and hinted)."""
        return max(self.rate_per_s, self.hint_per_s)

    def warm_target(
        self,
        *,
        horizon_s: float,
        min_warm: int,
        max_warm: int,
        worth_warming: bool,
    ) -> int:
        """Compute the cost-aware warm-session target.

        ``ceil(effective_rate × horizon)`` clamped into ``[floor, max_warm]``. The
        floor is ``min_warm`` when the provider's cold start is worth hiding, else
        ``0`` — a cheap-to-open provider holds no idle sessions. ``max_warm`` caps
        the spike. The result is never negative and never exceeds ``max_warm``.
        """
        floor = min_warm if worth_warming else 0
        ceiling = max(floor, max(0, max_warm))
        demanded = math.ceil(self.effective_rate_per_s * max(0.0, horizon_s))
        return max(floor, min(ceiling, demanded))


__all__ = ["DEFAULT_DEMAND_ALPHA", "DEFAULT_DEMAND_DECAY", "DemandModel"]
