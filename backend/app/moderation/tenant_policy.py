"""Per-tenant configurable moderation policy (§10).

Different deployments — a children's-reading product, an adult-fiction platform,
a museum kiosk — need different lines. A :class:`TenantPolicy` overrides the
baseline :data:`app.moderation.taxonomy.DEFAULT_DISPOSITIONS` on a per-category
basis and carries a few cross-cutting knobs (a global strictness multiplier, the
fail-open/closed posture for degraded classifications, the auto-takedown
threshold). It is a pure value object; persistence lives in the repository, and
the *resolved* policy is fed to the (pure) policy engine.

A tenant policy can only ever be **at least as strict** as the zero-tolerance
floor: it may tighten any category, but it can never relax a zero-tolerance
category (CSAM, extremism) below BLOCK — :meth:`rule_for` enforces that floor.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from app.moderation.taxonomy import (
    ZERO_TOLERANCE_CATEGORIES,
    CategoryRule,
    Disposition,
    ModerationCategory,
    Severity,
    default_rule,
)


class CategoryOverride(BaseModel):
    """A per-category override of the baseline disposition rule."""

    model_config = ConfigDict(frozen=True)

    flag_at: Severity | None = None
    block_at: Severity | None = None
    zero_tolerance: bool | None = None
    #: Hard allowlist: never flag/block this category for this tenant (still logged).
    allow: bool = False

    def resolve(self, base: CategoryRule, *, zero_tolerance_floor: bool) -> CategoryRule:
        """Apply this override onto ``base``, respecting the zero-tolerance floor."""
        if self.allow and not zero_tolerance_floor:
            # Effectively disable: flag/block only at an unreachable tier.
            return CategoryRule(Severity.CRITICAL, Severity.CRITICAL, zero_tolerance=False)
        zt = base.zero_tolerance if self.zero_tolerance is None else self.zero_tolerance
        if zero_tolerance_floor:
            zt = True  # can never be relaxed below zero-tolerance
        flag_at = self.flag_at if self.flag_at is not None else base.flag_at
        block_at = self.block_at if self.block_at is not None else base.block_at
        return CategoryRule(flag_at=flag_at, block_at=block_at, zero_tolerance=zt)


class TenantPolicy(BaseModel):
    """A tenant's resolved moderation policy (configurable; pure value object)."""

    model_config = ConfigDict(frozen=True)

    tenant_id: str = "default"
    version: str = "default"
    #: Per-category overrides; categories absent here use the baseline rule.
    overrides: dict[ModerationCategory, CategoryOverride] = Field(default_factory=dict)
    #: Scales every classifier score before bucketing (>1 = stricter). Clamped.
    strictness: float = 1.0
    #: When a classification is ``degraded`` (model error), should the gate fail
    #: open (treat as ALLOW) or closed (treat as BLOCK)? Surfaces choose, but this
    #: is the tenant-wide default the gate consults.
    fail_closed_on_degraded: bool = False
    #: A FLAG at/above this severity is auto-taken-down (queued + held) rather than
    #: merely surfaced. ``CRITICAL+1`` (unreachable) disables auto-takedown.
    auto_takedown_at: Severity = Severity.CRITICAL
    #: Whether FLAG content may still be *shown* while it waits for review. False
    #: means a FLAG behaves like a soft block at the gate (held).
    serve_flagged: bool = True

    def rule_for(self, category: ModerationCategory) -> CategoryRule:
        """The effective :class:`CategoryRule` for ``category`` after overrides + floor."""
        base = default_rule(category)
        zt_floor = category in ZERO_TOLERANCE_CATEGORIES
        override = self.overrides.get(category)
        if override is None:
            if zt_floor:
                return CategoryRule(base.flag_at, base.block_at, zero_tolerance=True)
            return base
        return override.resolve(base, zero_tolerance_floor=zt_floor)

    def scaled_severity(self, score: float) -> Severity:
        """Bucket a raw score after applying this tenant's strictness multiplier."""
        adjusted = max(0.0, min(1.0, score * max(0.1, min(5.0, self.strictness))))
        return Severity.from_score(adjusted)


#: The process-wide default policy (the conservative baseline).
DEFAULT_TENANT_POLICY = TenantPolicy(tenant_id="default", version="default")


def builtin_policies() -> dict[str, TenantPolicy]:
    """A small library of ready-made policies for common product shapes.

    These are the *seed* presets a deployment can adopt or clone; the repository
    persists per-tenant edits on top. They make the configurability concrete and
    give the tests fixed, named policies to assert against.
    """
    children = TenantPolicy(
        tenant_id="children",
        version="children-v1",
        strictness=1.4,
        fail_closed_on_degraded=True,
        serve_flagged=False,
        auto_takedown_at=Severity.HIGH,
        overrides={
            ModerationCategory.SEXUAL: CategoryOverride(
                flag_at=Severity.LOW, block_at=Severity.LOW
            ),
            ModerationCategory.VIOLENCE: CategoryOverride(
                flag_at=Severity.LOW, block_at=Severity.MEDIUM
            ),
            ModerationCategory.GORE: CategoryOverride(
                flag_at=Severity.LOW, block_at=Severity.LOW
            ),
            ModerationCategory.PROFANITY: CategoryOverride(
                flag_at=Severity.LOW, block_at=Severity.MEDIUM
            ),
            ModerationCategory.SELF_HARM: CategoryOverride(
                flag_at=Severity.LOW, block_at=Severity.LOW
            ),
        },
    )
    mature = TenantPolicy(
        tenant_id="mature",
        version="mature-v1",
        strictness=0.85,
        serve_flagged=True,
        overrides={
            # An adult-fiction platform tolerates depicted (non-minor) sexual
            # content and stylised violence, but the zero-tolerance floor still
            # blocks SEXUAL_MINORS / EXTREMISM no matter what is set here.
            ModerationCategory.SEXUAL: CategoryOverride(allow=True),
            ModerationCategory.VIOLENCE: CategoryOverride(
                flag_at=Severity.CRITICAL, block_at=Severity.CRITICAL
            ),
            ModerationCategory.PROFANITY: CategoryOverride(allow=True),
        },
    )
    return {
        DEFAULT_TENANT_POLICY.tenant_id: DEFAULT_TENANT_POLICY,
        children.tenant_id: children,
        mature.tenant_id: mature,
    }


def policy_blocks(policy: TenantPolicy, category: ModerationCategory, severity: Severity) -> bool:
    """Convenience: does ``policy`` block ``category`` at ``severity``?"""
    return policy.rule_for(category).disposition_for(severity) is Disposition.BLOCK


__all__ = [
    "DEFAULT_TENANT_POLICY",
    "CategoryOverride",
    "TenantPolicy",
    "builtin_policies",
    "policy_blocks",
]
