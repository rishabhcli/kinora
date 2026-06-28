"""Budget scopes, tiered caps, and alert levels (kinora.md §11.1).

Video-seconds are the scarce currency: a hard global ceiling, plus per-tenant,
per-session, and per-scene allocations so no one reader (or tenant) drains the
pool. The existing :class:`~app.memory.budget_service.BudgetService` enforces the
*hard* cap (it raises :class:`BudgetExceeded`). This module adds the **soft**
structure around that hard edge:

* :class:`BudgetScopeKind` — the four scopes a reservation can count against.
* :class:`AlertLevel` — a monotone severity ladder a usage fraction maps onto.
* :class:`TierThresholds` — the fractions of a cap at which each level trips
  (``info`` < ``warning`` < ``soft_cap`` < ``hard_cap``).
* :class:`TieredCap` — one scope's cap + thresholds, with the pure math that
  turns a ``(used, cap)`` pair into a usage fraction, an :class:`AlertLevel`, the
  headroom, and a "would this reservation cross soft?" check.
* :class:`BudgetTierPolicy` — the full set of caps (global/tenant/session/scene)
  assembled from :class:`Settings`, plus the per-scope evaluation used by the
  governor and the alerting layer.

Everything here is pure (no I/O), exhaustively unit-testable, and never raises on
ordinary input — the *enforcement* (raising) stays in ``BudgetService``.
"""

from __future__ import annotations

import enum
import math
from dataclasses import dataclass

from app.core.config import Settings

# --------------------------------------------------------------------------- #
# Scopes
# --------------------------------------------------------------------------- #


class BudgetScopeKind(enum.StrEnum):
    """The four scopes a video-seconds reservation can be counted against.

    ``global`` is the hard ceiling (§11.1). ``tenant`` is the multi-tenant
    allocation (one organization/owner). ``session`` and ``scene`` are the
    existing per-reader / per-scene allocations the budget service already keys.
    """

    GLOBAL = "global"
    TENANT = "tenant"
    SESSION = "session"
    SCENE = "scene"


#: Evaluation order: broadest cap first. A reservation must satisfy *every*
#: applicable scope, but reporting/alerts read them in this stable order.
SCOPE_ORDER: tuple[BudgetScopeKind, ...] = (
    BudgetScopeKind.GLOBAL,
    BudgetScopeKind.TENANT,
    BudgetScopeKind.SESSION,
    BudgetScopeKind.SCENE,
)


# --------------------------------------------------------------------------- #
# Alert levels
# --------------------------------------------------------------------------- #


class AlertLevel(enum.IntEnum):
    """A monotone severity ladder for budget pressure.

    ``IntEnum`` so callers can compare/escalate with ``>=`` and pick the max of a
    set of per-scope levels. ``OK`` is the floor; ``HARD_CAP`` means a further
    reservation would be refused by :class:`BudgetService`.
    """

    OK = 0
    INFO = 1
    WARNING = 2
    SOFT_CAP = 3
    HARD_CAP = 4

    @property
    def label(self) -> str:
        """Lower-case label for events/JSON (``ok``/``info``/.../``hard_cap``)."""
        return self.name.lower()

    @classmethod
    def from_label(cls, label: str) -> AlertLevel:
        """Parse a level back from its lower-case label (default :attr:`OK`)."""
        try:
            return cls[label.upper()]
        except KeyError:
            return cls.OK


# --------------------------------------------------------------------------- #
# Tier thresholds
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class TierThresholds:
    """Fractions of a cap [0, 1] at which each alert level trips.

    Invariant (enforced in :meth:`__post_init__`): ``info <= warning <= soft <=
    1.0``. ``hard`` is implicit at ``1.0`` (the cap itself). A usage fraction at or
    above a threshold trips that level; below ``info`` is :attr:`AlertLevel.OK`.
    """

    info: float = 0.50
    warning: float = 0.75
    soft: float = 0.90

    def __post_init__(self) -> None:
        for name, value in (("info", self.info), ("warning", self.warning), ("soft", self.soft)):
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"threshold {name}={value} must be within [0, 1]")
        if not self.info <= self.warning <= self.soft:
            raise ValueError(
                f"thresholds must be non-decreasing: info={self.info} "
                f"<= warning={self.warning} <= soft={self.soft}"
            )

    def level_for(self, fraction: float) -> AlertLevel:
        """Map a usage fraction (used/cap) to an :class:`AlertLevel`.

        ``>= 1.0`` is :attr:`AlertLevel.HARD_CAP` (the cap is met/exceeded).
        ``NaN`` (a zero cap) is treated as fully consumed -> :attr:`HARD_CAP`.
        """
        if math.isnan(fraction) or fraction >= 1.0:
            return AlertLevel.HARD_CAP
        if fraction >= self.soft:
            return AlertLevel.SOFT_CAP
        if fraction >= self.warning:
            return AlertLevel.WARNING
        if fraction >= self.info:
            return AlertLevel.INFO
        return AlertLevel.OK


# --------------------------------------------------------------------------- #
# A single scope's cap + thresholds
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class CapStatus:
    """The evaluated status of one cap against a current ``used`` value."""

    scope: BudgetScopeKind
    used_s: float
    cap_s: float
    fraction: float
    level: AlertLevel
    headroom_s: float
    soft_cap_s: float

    @property
    def at_or_over_soft(self) -> bool:
        """True once usage has reached the soft cap (degrade / warn loudly)."""
        return self.level >= AlertLevel.SOFT_CAP

    @property
    def exhausted(self) -> bool:
        """True once the hard cap is met (no further reservation fits)."""
        return self.level >= AlertLevel.HARD_CAP

    def as_dict(self) -> dict[str, object]:
        """JSON-serializable status (for the API + events)."""
        return {
            "scope": self.scope.value,
            "used_s": round(self.used_s, 3),
            "cap_s": round(self.cap_s, 3),
            "fraction": round(self.fraction, 4),
            "level": self.level.label,
            "headroom_s": round(self.headroom_s, 3),
            "soft_cap_s": round(self.soft_cap_s, 3),
        }


@dataclass(frozen=True, slots=True)
class TieredCap:
    """One scope's hard cap plus its tiered thresholds — pure cap math.

    The *hard* cap is ``cap_s``; the *soft* cap is ``cap_s * thresholds.soft``.
    None of these methods raise: a non-positive cap collapses to "fully consumed"
    so the scope reports :attr:`AlertLevel.HARD_CAP` rather than dividing by zero.
    """

    scope: BudgetScopeKind
    cap_s: float
    thresholds: TierThresholds = TierThresholds()

    @property
    def soft_cap_s(self) -> float:
        """The soft cap in absolute video-seconds."""
        return max(self.cap_s, 0.0) * self.thresholds.soft

    def fraction(self, used_s: float) -> float:
        """``used / cap`` clamped at the bottom to 0; ``inf`` cap -> 0; ``<=0`` cap -> 1."""
        if math.isinf(self.cap_s):
            return 0.0
        if self.cap_s <= 0.0:
            return 1.0
        return max(used_s, 0.0) / self.cap_s

    def headroom_s(self, used_s: float) -> float:
        """Remaining hard headroom (never negative)."""
        if math.isinf(self.cap_s):
            return math.inf
        return max(self.cap_s - max(used_s, 0.0), 0.0)

    def soft_headroom_s(self, used_s: float) -> float:
        """Remaining headroom under the *soft* cap (never negative)."""
        if math.isinf(self.cap_s):
            return math.inf
        return max(self.soft_cap_s - max(used_s, 0.0), 0.0)

    def evaluate(self, used_s: float) -> CapStatus:
        """The full :class:`CapStatus` for a current ``used`` value."""
        frac = self.fraction(used_s)
        return CapStatus(
            scope=self.scope,
            used_s=max(used_s, 0.0),
            cap_s=self.cap_s,
            fraction=frac,
            level=self.thresholds.level_for(frac),
            headroom_s=self.headroom_s(used_s),
            soft_cap_s=self.soft_cap_s,
        )

    def would_exceed_hard(self, used_s: float, requested_s: float) -> bool:
        """Would ``used + requested`` breach the hard cap? (mirrors BudgetService)."""
        if math.isinf(self.cap_s):
            return False
        return max(used_s, 0.0) + max(requested_s, 0.0) > self.cap_s

    def would_exceed_soft(self, used_s: float, requested_s: float) -> bool:
        """Would ``used + requested`` cross the soft cap (warn / prefer-degrade)?"""
        if math.isinf(self.cap_s):
            return False
        return max(used_s, 0.0) + max(requested_s, 0.0) > self.soft_cap_s


# --------------------------------------------------------------------------- #
# The full policy (all four scopes)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class BudgetTierPolicy:
    """The complete tiered-cap policy across all scopes.

    Assembled from :class:`Settings`. A tenant cap of ``inf`` means "no separate
    tenant allocation" (the global ceiling is the only ceiling) — useful for the
    single-tenant local/demo deployment.
    """

    global_cap: TieredCap
    tenant_cap: TieredCap
    session_cap: TieredCap
    scene_cap: TieredCap

    @classmethod
    def from_settings(cls, settings: Settings) -> BudgetTierPolicy:
        """Build the policy from application :class:`Settings` (additive fields)."""
        thresholds = TierThresholds(
            info=settings.finops_alert_info_fraction,
            warning=settings.finops_alert_warning_fraction,
            soft=settings.finops_soft_cap_fraction,
        )
        tenant_cap = settings.finops_tenant_ceiling_video_s
        return cls(
            global_cap=TieredCap(
                BudgetScopeKind.GLOBAL, settings.budget_ceiling_video_s, thresholds
            ),
            tenant_cap=TieredCap(
                BudgetScopeKind.TENANT,
                tenant_cap if tenant_cap > 0 else math.inf,
                thresholds,
            ),
            session_cap=TieredCap(
                BudgetScopeKind.SESSION, settings.budget_per_session_s, thresholds
            ),
            scene_cap=TieredCap(BudgetScopeKind.SCENE, settings.budget_per_scene_s, thresholds),
        )

    def cap_for(self, scope: BudgetScopeKind) -> TieredCap:
        """The :class:`TieredCap` for a scope."""
        return {
            BudgetScopeKind.GLOBAL: self.global_cap,
            BudgetScopeKind.TENANT: self.tenant_cap,
            BudgetScopeKind.SESSION: self.session_cap,
            BudgetScopeKind.SCENE: self.scene_cap,
        }[scope]

    def evaluate_all(self, used_by_scope: dict[BudgetScopeKind, float]) -> list[CapStatus]:
        """Evaluate every scope present in ``used_by_scope`` (in :data:`SCOPE_ORDER`)."""
        return [
            self.cap_for(scope).evaluate(used_by_scope[scope])
            for scope in SCOPE_ORDER
            if scope in used_by_scope
        ]

    @staticmethod
    def worst_level(statuses: list[CapStatus]) -> AlertLevel:
        """The most severe :class:`AlertLevel` across a set of cap statuses."""
        if not statuses:
            return AlertLevel.OK
        return max(s.level for s in statuses)

    @staticmethod
    def binding_scope(statuses: list[CapStatus]) -> CapStatus | None:
        """The scope with the least hard headroom (the binding constraint)."""
        finite = [s for s in statuses if not math.isinf(s.headroom_s)]
        if not finite:
            return None
        return min(finite, key=lambda s: s.headroom_s)


__all__ = [
    "SCOPE_ORDER",
    "AlertLevel",
    "BudgetScopeKind",
    "BudgetTierPolicy",
    "CapStatus",
    "TierThresholds",
    "TieredCap",
]
