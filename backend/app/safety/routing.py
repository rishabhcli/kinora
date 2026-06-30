"""Provider routing avoidance — pick providers that will not reject the content.

Given a prompt's findings and the per-provider policy profiles, compute a
:class:`~app.safety.contracts.RoutingPlan`: the viable providers (best-first) and
the ones to avoid (with the categories that ruled them out). The router uses this
to **skip a provider that would certainly reject the content**, saving a wasted
metered render, and to fall back to the most permissive viable lane (often the
ffmpeg Ken-Burns degradation over an already-approved keyframe).

Pure logic over the profile registry — no I/O, exhaustively testable.

Ordering: viable providers are sorted by (1) fewest *near-miss* categories (those
the provider tolerates only just), then (2) higher ``permissiveness`` (a faithful
adaptation is less likely to need re-softening there), then (3) provider name for
determinism. A provider with **any** refusing category is dropped from the ordered
list (it would fail), but it is still reported in ``rankings`` so the decision is
explainable.
"""

from __future__ import annotations

from app.safety.contracts import (
    Finding,
    ProviderRanking,
    RoutingPlan,
    SafetyCategory,
)
from app.safety.profiles import ProfileRegistry, ProviderPolicyProfile


def _worst_by_category(findings: list[Finding]) -> dict[SafetyCategory, Finding]:
    worst: dict[SafetyCategory, Finding] = {}
    for f in findings:
        if not f.positive:
            continue
        cur = worst.get(f.category)
        if cur is None or f.severity > cur.severity:
            worst[f.category] = f
    return worst


def _rank_provider(
    profile: ProviderPolicyProfile,
    worst: dict[SafetyCategory, Finding],
) -> ProviderRanking:
    """Rank one provider against the per-category worst findings."""
    refusing: list[SafetyCategory] = []
    near_misses = 0
    for cat, f in worst.items():
        if profile.refuses(cat, f.severity):
            refusing.append(cat)
        else:
            # "near miss": tolerated, but only one tier below the refusal line.
            threshold = profile.refusal_severity(cat)
            if int(threshold) - int(f.severity) <= 1:
                near_misses += 1
    viable = not refusing
    # Score: viable providers rank above non-viable; among viable, fewer near
    # misses and higher permissiveness win. Kept in a single comparable float so
    # the plan's ordering is stable and explainable.
    score = (
        (1000.0 if viable else 0.0)
        - near_misses * 10.0
        + profile.permissiveness
    )
    note = profile.note
    if refusing:
        cats = ", ".join(sorted(c.value for c in refusing))
        note = f"refuses: {cats}"
    return ProviderRanking(
        provider=profile.provider,
        viable=viable,
        score=round(score, 4),
        refusing_categories=sorted(refusing, key=lambda c: c.value),
        note=note,
    )


def plan_routing(
    findings: list[Finding],
    *,
    registry: ProfileRegistry | None = None,
    candidates: list[str] | None = None,
) -> RoutingPlan:
    """Compute the routing plan for a prompt's ``findings`` (pure).

    Args:
        findings: the (already-softened) prompt's findings.
        registry: the provider profile registry (builtin when ``None``).
        candidates: restrict to these provider ids (defaults to all registered).
    """
    reg = registry or ProfileRegistry.builtin()
    provider_ids = candidates if candidates is not None else reg.providers()
    worst = _worst_by_category(findings)

    rankings = [_rank_provider(reg.get(pid), worst) for pid in provider_ids]
    viable = [r for r in rankings if r.viable]
    # Best-first: higher score first, then provider name for stable ties.
    viable.sort(key=lambda r: (-r.score, r.provider))
    ordered = [r.provider for r in viable]

    avoided: dict[SafetyCategory, None] = {}
    for r in rankings:
        for cat in r.refusing_categories:
            avoided.setdefault(cat, None)

    if not ordered:
        if not worst:
            # No findings yet no viable provider ⇒ empty candidate set.
            reason = "no candidate providers"
        else:
            cats = ", ".join(sorted(c.value for c in worst))
            reason = f"every candidate provider refuses the content ({cats})"
    elif len(ordered) < len(provider_ids):
        reason = f"routing to {ordered[0]}; avoided {len(provider_ids) - len(ordered)} provider(s)"
    else:
        reason = f"all candidate providers viable; preferring {ordered[0]}"

    return RoutingPlan(
        ordered_providers=ordered,
        rankings=sorted(rankings, key=lambda r: (-r.score, r.provider)),
        avoided_categories=sorted(avoided, key=lambda c: c.value),
        reason=reason,
    )


__all__ = ["plan_routing"]
