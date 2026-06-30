"""The kill-switch safety layer — a guarded flag can only ever be forced *down*.

Some flags gate real-world spend or risk: ``KINORA_LIVE_VIDEO`` (Wan credits),
and any other knob an operator marks ``kill_switch=True``. The runtime plane must
guarantee that no override, targeting rule, or percentage rollout can ever
*raise* such a flag above its base (Settings) value — the runtime surface is
allowed to make things *safer* (force a kill-switch off, dial a ramp down) but
never riskier.

:class:`KillSwitchGuard` is the single chokepoint that enforces this. It is
applied in two places:

* **on write** — :class:`~app.flags.plane.plane.RuntimeConfigPlane` validates
  every override / rule value through :meth:`check`, raising
  :class:`KillSwitchViolation` so a bad write is rejected with a 4xx and never
  persisted;
* **on read** — the resolver clamps the resolved value through :meth:`clamp` as
  a belt-and-suspenders backstop, so even a value that somehow got into the layer
  (a hand-edited dict, a future bug) can never be *served* raised.

"Down" is defined per type: for a ``BOOL`` kill-switch the only safe direction is
``True -> False`` (so ``False`` can never become ``True``); for numeric
kill-switches a lower number is safer (e.g. a spend ceiling). The rule is
intentionally conservative: anything ambiguous (string/json) is pinned to the
base.
"""

from __future__ import annotations

from app.flags.plane.errors import KillSwitchViolation
from app.flags.plane.spec import FlagSpec, FlagType, FlagValue


class KillSwitchGuard:
    """Enforces that guarded kill-switches can only be forced toward "safe"."""

    def is_safe(self, spec: FlagSpec, base: FlagValue, candidate: FlagValue) -> bool:
        """Whether serving ``candidate`` (vs ``base``) keeps the flag no riskier.

        Non-kill-switch flags are always safe (returns ``True``). For a guarded
        flag, ``candidate`` is safe iff it does not raise the flag above ``base``:

        * ``BOOL`` — safe unless it flips ``False`` base to ``True``.
        * ``INT`` / ``FLOAT`` — safe iff ``candidate <= base`` (lower = safer,
          the kill-switch convention for spend ceilings / concurrency caps).
        * ``STRING`` / ``JSON`` — safe only if equal to ``base`` (no ordering to
          reason about, so a guarded non-scalar may not be changed at all).
        * ``None`` candidate — always safe (means "no opinion; use base").
        """
        if not spec.kill_switch:
            return True
        if candidate is None:
            return True
        match spec.type:
            case FlagType.BOOL:
                # Unsafe only when raising a False base to True.
                return not (base is False and candidate is True)
            case FlagType.INT | FlagType.FLOAT:
                return _as_number(candidate) <= _as_number(base)
            case FlagType.STRING | FlagType.JSON:
                return candidate == base
        return False  # pragma: no cover - exhaustive match

    def check(self, spec: FlagSpec, base: FlagValue, candidate: FlagValue) -> None:
        """Raise :class:`KillSwitchViolation` if ``candidate`` would raise ``spec``."""
        if not self.is_safe(spec, base, candidate):
            raise KillSwitchViolation(spec.key, base, candidate)

    def clamp(self, spec: FlagSpec, base: FlagValue, candidate: FlagValue) -> FlagValue:
        """Return ``candidate`` if safe, else fall back to the safe ``base``.

        Used on the read path so a resolved value is *never* served raised, even
        if validation was bypassed. The clamp is silent (the caller gets the safe
        value); callers that want to know about the violation use :meth:`check`.
        """
        if self.is_safe(spec, base, candidate):
            return candidate
        return base


def _as_number(value: FlagValue) -> float:
    if isinstance(value, bool):  # defensive: bool is an int subclass
        return float(int(value))
    if isinstance(value, int | float):
        return float(value)
    raise KillSwitchViolation("<numeric>", value, value)


__all__ = ["KillSwitchGuard"]
