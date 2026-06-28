"""Resolve a :class:`~app.flags.models.Rollout` to a concrete variation key.

This is the deterministic split: given a rollout (weighted variations + a
bucketing dimension + a salt) and a context, pick exactly which variation the
context's bucketing unit lands on. Pure and stable — the same unit always lands
on the same variation for a fixed salt.

Also provides :func:`progressive_percent`, the time-driven schedule helper for a
gradual rollout (10% today → 50% next week → 100%) so a flag can ramp without an
operator hand-editing weights every step.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.flags.context import EvalContext
from app.flags.hashing import weighted_index
from app.flags.models import Rollout


def resolve_rollout(rollout: Rollout, context: EvalContext, *, default_salt: str) -> str:
    """Return the variation key ``context`` lands on within ``rollout``.

    The salt precedence is: ``rollout.salt`` if set, else ``default_salt``
    (normally the flag key). ``rollout.seed`` is appended so an operator can
    reshuffle the assignment without changing the salt's meaning. The bucketing
    unit is resolved from ``rollout.bucket_by`` (``None`` → the context key).
    """
    salt = rollout.salt or default_salt
    if rollout.seed:
        salt = f"{salt}#{rollout.seed}"
    unit = context.unit_for(rollout.bucket_by)
    weights = tuple(w.weight for w in rollout.weights)
    index = weighted_index(unit, salt, weights)
    return rollout.weights[index].variation


@dataclass(frozen=True, slots=True)
class RampStep:
    """One step of a progressive rollout schedule: at ``at_epoch_s`` go ``percent``."""

    at_epoch_s: float
    percent: float


def progressive_percent(steps: tuple[RampStep, ...], now_epoch_s: float) -> float:
    """The active rollout percent at ``now`` given an ascending step schedule.

    Returns the ``percent`` of the latest step whose ``at_epoch_s <= now``; if
    no step has started yet, returns ``0.0``. Steps need not be pre-sorted.
    Clamped to ``[0, 100]``.
    """
    active = 0.0
    started = False
    for step in sorted(steps, key=lambda s: s.at_epoch_s):
        if step.at_epoch_s <= now_epoch_s:
            active = step.percent
            started = True
    if not started:
        return 0.0
    return max(0.0, min(100.0, active))


__all__ = ["RampStep", "progressive_percent", "resolve_rollout"]
