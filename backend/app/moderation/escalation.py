"""Rate-of-violation tracking + repeat-offender escalation ladder (§10).

A single block is a content decision; a *pattern* of blocks is an actor decision.
This module turns recorded violations into an escalating enforcement posture:

    tier 0  clean
    tier 1  warned        (a notice; no functional change)
    tier 2  throttled     (generation rate-limited; flagged content held, not served)
    tier 3  suspended     (generation suspended for a cooldown window)
    tier 4  banned        (generation blocked until manual reinstatement)

The ladder is driven by a **rolling window** count: violations inside the window
accumulate; when the count crosses a tier threshold the actor advances. An old
burst decays — once the window elapses with no new violations, the count resets
and the tier can step back down (except a manual ban, which is sticky until
reinstated). The threshold/window/decay are :class:`EscalationPolicy` knobs so a
tenant can tune leniency.

:func:`compute_tier` and :func:`next_window` are **pure** functions of the
counters + clock, so the ladder is exhaustively unit-testable without a DB. The
:class:`EscalationService` then persists the tally + writes the audit trail.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from app.core.logging import get_logger
from app.moderation.audit import AuditAction, ModerationAuditLog
from app.moderation.models import ViolationCounter
from app.moderation.repositories import ViolationCounterRepo
from app.moderation.taxonomy import ModerationCategory, Severity

if TYPE_CHECKING:
    pass

logger = get_logger("app.moderation.escalation")


class EnforcementTier(enum.IntEnum):
    """The repeat-offender enforcement ladder (ordered)."""

    CLEAN = 0
    WARNED = 1
    THROTTLED = 2
    SUSPENDED = 3
    BANNED = 4

    @property
    def label(self) -> str:
        return self.name.lower()


@dataclass(frozen=True, slots=True)
class EscalationPolicy:
    """Tunable thresholds for the escalation ladder.

    Args:
        window: the rolling window over which violations accumulate.
        warn_at / throttle_at / suspend_at / ban_at: window-count thresholds at
            which the actor reaches each tier (must be non-decreasing).
        suspend_cooldown: how long a SUSPENDED actor stays generation-suspended.
        severity_weight: a CRITICAL violation counts as this many ordinary ones,
            so a single severe abuse can escalate faster than a trickle of minor
            flags (clamped to ≥ 1).
    """

    window: timedelta = timedelta(hours=24)
    warn_at: int = 1
    throttle_at: int = 3
    suspend_at: int = 5
    ban_at: int = 8
    suspend_cooldown: timedelta = timedelta(hours=24)
    severity_weight: int = 2

    def weight_for(self, severity: Severity) -> int:
        """How many window-units a violation at ``severity`` contributes (≥1)."""
        if severity >= Severity.CRITICAL:
            return max(1, self.severity_weight)
        if severity >= Severity.HIGH:
            return max(1, self.severity_weight - 1)
        return 1


DEFAULT_ESCALATION_POLICY = EscalationPolicy()


def compute_tier(window_count: int, policy: EscalationPolicy) -> EnforcementTier:
    """The enforcement tier for a given in-window count (pure)."""
    if window_count >= policy.ban_at:
        return EnforcementTier.BANNED
    if window_count >= policy.suspend_at:
        return EnforcementTier.SUSPENDED
    if window_count >= policy.throttle_at:
        return EnforcementTier.THROTTLED
    if window_count >= policy.warn_at:
        return EnforcementTier.WARNED
    return EnforcementTier.CLEAN


def window_expired(window_started_at: datetime, now: datetime, policy: EscalationPolicy) -> bool:
    """Whether the rolling window has elapsed since it started (pure)."""
    return now - _aware(window_started_at) >= policy.window


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class EscalationOutcome:
    """The result of recording a violation: the new tier + enforcement flags."""

    actor_id: str
    tier: EnforcementTier
    window_count: int
    total_count: int
    suspended_until: datetime | None
    escalated: bool  # the tier increased on this event

    @property
    def generation_blocked(self) -> bool:
        """Whether the actor is currently barred from new generation."""
        if self.tier is EnforcementTier.BANNED:
            return True
        if self.suspended_until is None:
            return False
        return _aware(self.suspended_until) > datetime.now(UTC)

    @property
    def throttled(self) -> bool:
        """Whether the actor's generation should be rate-limited / not served."""
        return self.tier >= EnforcementTier.THROTTLED


class EscalationService:
    """Persist the rolling tally + drive the repeat-offender ladder (+ audit)."""

    def __init__(
        self,
        repo: ViolationCounterRepo,
        audit: ModerationAuditLog,
        *,
        policy: EscalationPolicy = DEFAULT_ESCALATION_POLICY,
    ) -> None:
        self._repo = repo
        self._audit = audit
        self._policy = policy

    async def record_violation(
        self,
        *,
        tenant_id: str,
        actor_id: str,
        severity: Severity,
        categories: list[ModerationCategory] | None = None,
        source: str = "gate",
        now: datetime | None = None,
    ) -> EscalationOutcome:
        """Count one violation against ``actor_id`` and (re)compute the tier."""
        at = now or datetime.now(UTC)
        row = await self._repo.get_or_create(tenant_id, actor_id, now=at)
        prev_tier = EnforcementTier(row.tier)

        # Roll the window forward if it has elapsed (an old burst decays).
        if window_expired(row.window_started_at, at, self._policy):
            row.window_count = 0
            row.window_started_at = at

        weight = self._policy.weight_for(severity)
        row.window_count += weight
        row.total_count += 1
        row.last_violation_at = at

        new_tier = compute_tier(row.window_count, self._policy)
        # A manual ban is sticky; otherwise the computed tier wins.
        if prev_tier is EnforcementTier.BANNED and new_tier < EnforcementTier.BANNED:
            new_tier = EnforcementTier.BANNED
        row.tier = int(new_tier)

        if new_tier is EnforcementTier.SUSPENDED and (
            row.suspended_until is None or _aware(row.suspended_until) <= at
        ):
            row.suspended_until = at + self._policy.suspend_cooldown
        elif new_tier is EnforcementTier.BANNED:
            # No expiry — a ban requires manual reinstatement.
            row.suspended_until = None

        await self._repo.save(row)
        escalated = new_tier > prev_tier
        if escalated:
            await self._audit.record(
                tenant_id=tenant_id,
                action=AuditAction.ESCALATE,
                actor_id="system",
                target_id=actor_id,
                payload={
                    "from_tier": prev_tier.label,
                    "to_tier": new_tier.label,
                    "window_count": row.window_count,
                    "severity": int(severity),
                    "source": source,
                },
            )
            if new_tier in {EnforcementTier.SUSPENDED, EnforcementTier.BANNED}:
                await self._audit.record(
                    tenant_id=tenant_id,
                    action=AuditAction.SUSPEND,
                    actor_id="system",
                    target_id=actor_id,
                    payload={
                        "tier": new_tier.label,
                        "until": row.suspended_until.isoformat()
                        if row.suspended_until
                        else None,
                    },
                )
        return _outcome(row)

    async def status(
        self, *, tenant_id: str, actor_id: str, now: datetime | None = None
    ) -> EscalationOutcome:
        """The actor's current enforcement posture (decaying the window if elapsed)."""
        at = now or datetime.now(UTC)
        row = await self._repo.get(tenant_id, actor_id)
        if row is None:
            return EscalationOutcome(
                actor_id=actor_id,
                tier=EnforcementTier.CLEAN,
                window_count=0,
                total_count=0,
                suspended_until=None,
                escalated=False,
            )
        if window_expired(row.window_started_at, at, self._policy):
            # Reflect the decay in the read without forcing a write.
            decayed_tier = EnforcementTier(row.tier)
            if decayed_tier is not EnforcementTier.BANNED:
                decayed_tier = EnforcementTier.CLEAN
            return EscalationOutcome(
                actor_id=actor_id,
                tier=decayed_tier,
                window_count=0,
                total_count=row.total_count,
                suspended_until=row.suspended_until,
                escalated=False,
            )
        return _outcome(row)

    async def reinstate(
        self, *, tenant_id: str, actor_id: str, reviewer_id: str, now: datetime | None = None
    ) -> EscalationOutcome:
        """Manually clear a suspension/ban (an appeal grant or amnesty)."""
        at = now or datetime.now(UTC)
        row = await self._repo.get_or_create(tenant_id, actor_id, now=at)
        row.tier = int(EnforcementTier.CLEAN)
        row.window_count = 0
        row.window_started_at = at
        row.suspended_until = None
        await self._repo.save(row)
        await self._audit.record(
            tenant_id=tenant_id,
            action=AuditAction.REINSTATE,
            actor_id=reviewer_id,
            target_id=actor_id,
            payload={"reinstated": True},
        )
        return _outcome(row)

    async def offenders(self, tenant_id: str, *, min_tier: int = 1) -> list[EscalationOutcome]:
        rows = await self._repo.list_offenders(tenant_id, min_tier=min_tier)
        return [_outcome(r) for r in rows]


def _outcome(row: ViolationCounter) -> EscalationOutcome:
    return EscalationOutcome(
        actor_id=row.actor_id,
        tier=EnforcementTier(row.tier),
        window_count=row.window_count,
        total_count=row.total_count,
        suspended_until=row.suspended_until,
        escalated=False,
    )


__all__ = [
    "DEFAULT_ESCALATION_POLICY",
    "EnforcementTier",
    "EscalationOutcome",
    "EscalationPolicy",
    "EscalationService",
    "compute_tier",
    "window_expired",
]
