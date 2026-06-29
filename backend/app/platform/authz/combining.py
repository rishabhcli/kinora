"""Decision-combining algorithms — fold many engine opinions into one verdict.

When the unified ``check()`` SDK runs several engines (RBAC, ABAC, the policy
DSL, the Zanzibar relationship checker), each returns an :class:`EngineResult`
with a three-valued :class:`Effect`. A **combining algorithm** (the XACML term)
folds that list into one final :class:`Decision`. The plane ships the four
standard algorithms so a deployment can pick the posture it wants:

* ``DENY_OVERRIDES`` (the safe default) — any explicit DENY wins, even over an
  ALLOW; otherwise an ALLOW wins; otherwise the request defaults (deny).
* ``PERMIT_OVERRIDES`` — any ALLOW wins, even over a DENY (use sparingly).
* ``FIRST_APPLICABLE`` — the first engine with a decisive (non-ABSTAIN) opinion
  wins; order matters (used when engines are layered by precedence).
* ``DENY_UNLESS_PERMIT`` — like deny-overrides but the *default* on all-ABSTAIN
  is explicit DENY (closed-world); pairs with a base-allow policy.

Every algorithm records the **full reason trail** (every engine's reasons, in
order) regardless of who won, so the decision log can explain not just the
verdict but every opinion that fed it. Obligations from the *winning* ALLOW
path are carried onto the decision; obligations attached to engines that did not
contribute to a grant are dropped (an obligation only makes sense on a permit).
"""

from __future__ import annotations

import enum
from collections.abc import Sequence

from app.platform.authz.model import (
    AuthorizationRequest,
    Decision,
    Effect,
    EngineResult,
    Obligation,
    Reason,
)


class CombiningAlgorithm(enum.StrEnum):
    """How a list of engine opinions collapses into one effect."""

    DENY_OVERRIDES = "deny_overrides"
    PERMIT_OVERRIDES = "permit_overrides"
    FIRST_APPLICABLE = "first_applicable"
    DENY_UNLESS_PERMIT = "deny_unless_permit"


def _collect_reasons(results: Sequence[EngineResult]) -> tuple[Reason, ...]:
    """Flatten every engine's reasons, preserving order (the full trail)."""
    out: list[Reason] = []
    for result in results:
        out.extend(result.reasons)
    return tuple(out)


def _allow_obligations(results: Sequence[EngineResult]) -> tuple[Obligation, ...]:
    """Obligations from every engine that voted ALLOW (deduplicated by identity)."""
    seen: set[tuple[str, tuple[tuple[str, object], ...]]] = set()
    out: list[Obligation] = []
    for result in results:
        if result.effect is not Effect.ALLOW:
            continue
        for ob in result.obligations:
            key = (ob.name, tuple(sorted(ob.parameters.items())))
            if key not in seen:
                seen.add(key)
                out.append(ob)
    return tuple(out)


def _resolve_effect(
    results: Sequence[EngineResult], algorithm: CombiningAlgorithm
) -> Effect:
    """Compute the final effect for ``results`` under ``algorithm``."""
    effects = [r.effect for r in results]
    has_allow = Effect.ALLOW in effects
    has_deny = Effect.DENY in effects

    if algorithm is CombiningAlgorithm.DENY_OVERRIDES:
        if has_deny:
            return Effect.DENY
        if has_allow:
            return Effect.ALLOW
        return Effect.DENY  # all-abstain → default deny

    if algorithm is CombiningAlgorithm.PERMIT_OVERRIDES:
        if has_allow:
            return Effect.ALLOW
        if has_deny:
            return Effect.DENY
        return Effect.DENY

    if algorithm is CombiningAlgorithm.FIRST_APPLICABLE:
        for effect in effects:
            if effect.is_decisive:
                return effect
        return Effect.DENY

    # DENY_UNLESS_PERMIT — closed-world: only an explicit ALLOW grants.
    return Effect.ALLOW if has_allow and not has_deny else Effect.DENY


def combine(
    request: AuthorizationRequest,
    results: Sequence[EngineResult],
    *,
    algorithm: CombiningAlgorithm = CombiningAlgorithm.DENY_OVERRIDES,
) -> Decision:
    """Fold a list of :class:`EngineResult` into one :class:`Decision`.

    The default algorithm is :attr:`CombiningAlgorithm.DENY_OVERRIDES` — the
    safe posture in which any explicit DENY beats any ALLOW. The full reason
    trail from every engine is always preserved on the decision.
    """
    effect = _resolve_effect(results, algorithm)
    reasons = _collect_reasons(results)
    obligations = _allow_obligations(results) if effect is Effect.ALLOW else ()
    # When the effect resolved from an all-abstain set, annotate why.
    if not reasons:
        reasons = (
            Reason(
                source="combiner",
                effect=effect,
                detail=f"no engine had an opinion; {algorithm.value} default",
            ),
        )
    return Decision(
        request=request,
        effect=effect,
        reasons=reasons,
        obligations=obligations,
    )


__all__ = ["CombiningAlgorithm", "combine"]
