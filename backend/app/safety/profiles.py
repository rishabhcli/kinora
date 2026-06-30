"""Per-provider POLICY PROFILES — what each video/image model refuses.

Clips come from several providers, each with its own content policy. Sending a
prompt to a provider that will certainly reject it wastes a metered render and a
round-trip. A :class:`ProviderPolicyProfile` encodes, per category, the severity
at which a given provider *refuses* the content (and optionally *quarantines* it
for the provider's own review). The router (:mod:`app.safety.routing`) consults
the registry to **avoid** providers that would reject a softened prompt, falling
back to the most permissive viable provider — fewer wasted spends, no silent
policy violations.

The profiles here are deliberately conservative, illustrative defaults keyed to
the providers Kinora actually ships against (DashScope Wan, MiniMax Hailuo, the
ffmpeg Ken-Burns degradation lane, and a permissive self-host placeholder). They
are *data*, swappable per deployment, and they can never relax the zero-tolerance
floor — :func:`ProviderPolicyProfile.refusal_severity` clamps CSAM / extremism to
refuse at any positive severity regardless of the profile entry.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.safety.taxonomy import ZERO_TOLERANCE_CATEGORIES, SafetyCategory, Severity


@dataclass(frozen=True)
class ProviderPolicyProfile:
    """One provider's content policy, as severity thresholds per category.

    Args:
        provider: the provider id (matches the router's backend names).
        refuses_at: per-category severity at/above which the provider *refuses*
            (the request would fail / be rejected). A category absent from the map
            uses ``default_refuses_at``.
        default_refuses_at: the fallback refusal threshold for unlisted categories.
        permissiveness: a 0..1 tie-breaker the router prefers (higher = more
            permissive ⇒ a faithful adaptation is less likely to need re-softening
            on this provider). Cosmetic ordering only; never overrides viability.
        note: human-facing description for the explainable routing plan.
    """

    provider: str
    refuses_at: dict[SafetyCategory, Severity] = field(default_factory=dict)
    default_refuses_at: Severity = Severity.HIGH
    permissiveness: float = 0.5
    note: str = ""

    def refusal_severity(self, category: SafetyCategory) -> Severity:
        """The severity at which this provider refuses ``category``.

        The zero-tolerance floor is clamped here: no profile, however permissive,
        can claim a provider tolerates CSAM / violent extremism — those always
        refuse at any positive severity.
        """
        if category in ZERO_TOLERANCE_CATEGORIES:
            return Severity.LOW
        return self.refuses_at.get(category, self.default_refuses_at)

    def refuses(self, category: SafetyCategory, severity: Severity) -> bool:
        """True when this provider would refuse a finding of ``category``@``severity``."""
        if severity <= Severity.NONE:
            return False
        return severity >= self.refusal_severity(category)


#: Built-in, illustrative profiles for the providers Kinora ships against.
#: DashScope Wan is the strictest hosted lane; MiniMax is moderately strict; the
#: ffmpeg Ken-Burns "degrade" lane renders from an already-approved keyframe so it
#: is the most permissive *render* lane (the keyframe itself is screened upstream);
#: "selfhost" is a permissive placeholder for a future self-hosted model.
BUILTIN_PROFILES: tuple[ProviderPolicyProfile, ...] = (
    ProviderPolicyProfile(
        provider="dashscope",
        refuses_at={
            SafetyCategory.SEXUAL: Severity.MEDIUM,
            SafetyCategory.NUDITY: Severity.MEDIUM,
            SafetyCategory.GORE: Severity.MEDIUM,
            SafetyCategory.VIOLENCE: Severity.HIGH,
            SafetyCategory.HATE: Severity.LOW,
            SafetyCategory.HARASSMENT: Severity.MEDIUM,
            SafetyCategory.SELF_HARM: Severity.MEDIUM,
            SafetyCategory.MINORS: Severity.HIGH,
            SafetyCategory.WEAPONS: Severity.MEDIUM,
            SafetyCategory.DRUGS: Severity.MEDIUM,
        },
        default_refuses_at=Severity.HIGH,
        permissiveness=0.4,
        note="Hosted DashScope Wan — strict on sexual/nudity/gore.",
    ),
    ProviderPolicyProfile(
        provider="minimax",
        refuses_at={
            SafetyCategory.SEXUAL: Severity.MEDIUM,
            SafetyCategory.NUDITY: Severity.HIGH,
            SafetyCategory.GORE: Severity.HIGH,
            SafetyCategory.VIOLENCE: Severity.CRITICAL,
            SafetyCategory.HATE: Severity.LOW,
            SafetyCategory.HARASSMENT: Severity.MEDIUM,
            SafetyCategory.SELF_HARM: Severity.MEDIUM,
            SafetyCategory.MINORS: Severity.HIGH,
        },
        default_refuses_at=Severity.HIGH,
        permissiveness=0.6,
        note="Hosted MiniMax Hailuo — more tolerant of stylised violence.",
    ),
    ProviderPolicyProfile(
        provider="degrade",
        refuses_at={
            # The Ken-Burns lane pans over an already-approved keyframe, so it only
            # refuses the zero-tolerance floor (clamped) and never re-judges the
            # still — it cannot generate new content.
        },
        default_refuses_at=Severity.CRITICAL,
        permissiveness=0.95,
        note="ffmpeg Ken-Burns degradation over a screened keyframe — most permissive.",
    ),
    ProviderPolicyProfile(
        provider="selfhost",
        refuses_at={
            SafetyCategory.MINORS: Severity.HIGH,
        },
        default_refuses_at=Severity.CRITICAL,
        permissiveness=0.85,
        note="Self-hosted lane placeholder — permissive (still floors zero-tolerance).",
    ),
)


class ProfileRegistry:
    """A registry of provider policy profiles the router queries.

    Built from :data:`BUILTIN_PROFILES` by default; a deployment can register or
    replace a profile (e.g. when a provider tightens its policy). Lookup falls back
    to a conservative default profile so an unknown provider is treated as *strict*
    rather than silently permissive.
    """

    def __init__(self, profiles: list[ProviderPolicyProfile] | None = None) -> None:
        self._profiles: dict[str, ProviderPolicyProfile] = {}
        for profile in profiles if profiles is not None else BUILTIN_PROFILES:
            self._profiles[profile.provider] = profile

    @classmethod
    def builtin(cls) -> ProfileRegistry:
        return cls(list(BUILTIN_PROFILES))

    def register(self, profile: ProviderPolicyProfile) -> None:
        self._profiles[profile.provider] = profile

    def get(self, provider: str) -> ProviderPolicyProfile:
        existing = self._profiles.get(provider)
        if existing is not None:
            return existing
        # Unknown provider: treat conservatively (strict) so we never *assume* a
        # provider is permissive and waste a render finding out it is not.
        return ProviderPolicyProfile(
            provider=provider,
            default_refuses_at=Severity.MEDIUM,
            permissiveness=0.0,
            note="unknown provider — treated as strict",
        )

    def providers(self) -> list[str]:
        return list(self._profiles)

    def all(self) -> list[ProviderPolicyProfile]:
        return list(self._profiles.values())


__all__ = [
    "BUILTIN_PROFILES",
    "ProfileRegistry",
    "ProviderPolicyProfile",
]
