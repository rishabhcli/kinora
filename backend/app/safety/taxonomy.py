"""The safety-gateway vocabulary — categories, severity, and gateway actions.

This is the small, ordered, I/O-free vocabulary every other layer speaks. It is
deliberately *separate* from :mod:`app.moderation.taxonomy`: the moderation
subsystem decides whether content may *exist / be shown* (ALLOW / FLAG / BLOCK
into a human-review queue); the **safety gateway** decides what the generation
pipeline should *do with a render request*, which adds a fourth, pipeline-native
action — ``TRANSFORM`` (auto-soften the prompt and proceed) — and treats
``QUARANTINE`` as the post-generation hold for a generated clip.

Three orthogonal axes:

* :class:`SafetyCategory` — *what kind* of policy concern a finding is about.
* :class:`Severity` — *how bad* a finding is, on an ordered 0–4 scale (an
  ``IntEnum`` so thresholds compare with ``>=``).
* :class:`SafetyAction` — *what the gateway does*: ``ALLOW`` / ``TRANSFORM`` /
  ``QUARANTINE`` / ``BLOCK``, ordered by strictness so the gateway can take the
  strictest action across many findings.

:data:`DEFAULT_POLICY` is the conservative baseline mapping each category to the
severity thresholds at which a finding becomes transformable / blockable, plus
the *zero-tolerance floor* (CSAM, violent extremism) that no provider profile or
director override can relax.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass


class SafetyCategory(enum.StrEnum):
    """A policy concern a finding can be *about* (generation-pipeline guardrails).

    Mirrors the common safety-taxonomy families so a real model classifier's
    native labels map on cleanly, and lines up name-for-name with
    :class:`app.moderation.taxonomy.ModerationCategory` where they overlap so the
    two subsystems can share a classifier without translation. ``SAFE`` is the
    explicit "nothing fired" label so a classifier always returns ≥1 finding.
    """

    SAFE = "safe"
    SEXUAL = "sexual"
    #: Sexual content involving / sexualising minors — always zero-tolerance.
    SEXUAL_MINORS = "sexual_minors"
    MINORS = "minors"  # non-sexual but sensitive depiction of minors
    VIOLENCE = "violence"
    GORE = "gore"
    HATE = "hate"
    HARASSMENT = "harassment"
    SELF_HARM = "self_harm"
    EXTREMISM = "extremism"  # terrorism / violent extremist content
    WEAPONS = "weapons"  # instructional weapon-making
    DRUGS = "drugs"
    PROFANITY = "profanity"
    NUDITY = "nudity"  # non-sexual nudity (provider profiles vary widely here)
    SUBSTANCE = "substance"  # alcohol / tobacco / drug *use* depiction (advisory)
    FRIGHTENING = "frightening"  # horror / intense imagery (advisory axis)
    OTHER = "other"


class Severity(enum.IntEnum):
    """Ordered severity of a single finding (higher = worse).

    An ``IntEnum`` so policy thresholds can be compared with ``>=``.
    """

    NONE = 0
    LOW = 1
    MEDIUM = 2
    HIGH = 3
    CRITICAL = 4

    @classmethod
    def from_score(cls, score: float) -> Severity:
        """Bucket a 0..1 classifier confidence/severity score into a tier.

        ``[0,0.2)→NONE``, ``[0.2,0.4)→LOW``, ``[0.4,0.6)→MEDIUM``,
        ``[0.6,0.85)→HIGH``, ``[0.85,1]→CRITICAL``. Clamped to ``[0,1]``. Matches
        :meth:`app.moderation.taxonomy.Severity.from_score` exactly so scores are
        portable between the two subsystems.
        """
        s = max(0.0, min(1.0, score))
        if s < 0.2:
            return cls.NONE
        if s < 0.4:
            return cls.LOW
        if s < 0.6:
            return cls.MEDIUM
        if s < 0.85:
            return cls.HIGH
        return cls.CRITICAL


class SafetyAction(enum.StrEnum):
    """What the gateway decides to *do* with a render request or generated clip.

    Ordered by strictness via :attr:`rank` so the gateway can take the strictest
    action across many findings.

    * ``ALLOW`` — proceed unchanged.
    * ``TRANSFORM`` — auto-soften the prompt (intent-preserving) and proceed. Only
      meaningful pre-generation; the softener records exactly what it changed.
    * ``QUARANTINE`` — hold for human review (a generated clip is not shown; a
      prompt is parked) — the content *might* be admissible but a person decides.
    * ``BLOCK`` — refuse outright; never sent to a provider / never shown.
    """

    ALLOW = "allow"
    TRANSFORM = "transform"
    QUARANTINE = "quarantine"
    BLOCK = "block"

    @property
    def rank(self) -> int:
        """Strictness rank (BLOCK > QUARANTINE > TRANSFORM > ALLOW)."""
        return {
            SafetyAction.ALLOW: 0,
            SafetyAction.TRANSFORM: 1,
            SafetyAction.QUARANTINE: 2,
            SafetyAction.BLOCK: 3,
        }[self]

    @classmethod
    def strictest(cls, actions: list[SafetyAction]) -> SafetyAction:
        """The strictest action in the list (ALLOW if empty)."""
        return max(actions, default=cls.ALLOW, key=lambda a: a.rank)


@dataclass(frozen=True, slots=True)
class CategoryPolicy:
    """The baseline policy for one category.

    Args:
        transform_at: severity at/above which a finding is *transformable* — the
            softener will try to rewrite it; if softening fails the finding falls
            through to ``block_at`` / ``quarantine_at``.
        quarantine_at: severity at/above which a finding is held for review.
        block_at: severity at/above which a finding is blocked outright.
        zero_tolerance: if True, *any* positive finding (severity > NONE) BLOCKs,
            regardless of the thresholds (CSAM, violent extremism). Never relaxable.
        softenable: whether the softener is *allowed* to attempt a rewrite for this
            category (literary violence/sexuality/gore: yes; hate/CSAM: never —
            you cannot "tastefully frame" a slur or child abuse).
    """

    transform_at: Severity
    quarantine_at: Severity
    block_at: Severity
    zero_tolerance: bool = False
    softenable: bool = False

    def action_for(self, severity: Severity, *, allow_transform: bool) -> SafetyAction:
        """Resolve this category's gateway action for a finding at ``severity``.

        ``allow_transform`` lets the *output* gate (where there is no prompt to
        rewrite) collapse ``TRANSFORM`` into ``QUARANTINE``.
        """
        if severity <= Severity.NONE:
            return SafetyAction.ALLOW
        if self.zero_tolerance:
            return SafetyAction.BLOCK
        if severity >= self.block_at:
            return SafetyAction.BLOCK
        if severity >= self.quarantine_at:
            return SafetyAction.QUARANTINE
        if severity >= self.transform_at:
            if allow_transform and self.softenable:
                return SafetyAction.TRANSFORM
            return SafetyAction.QUARANTINE
        return SafetyAction.ALLOW


#: The conservative baseline gateway policy. Zero-tolerance categories block on any
#: positive finding; literary categories (violence/gore/sexual/nudity) prefer a
#: TRANSFORM before escalating; hate/harassment/self-harm never soften.
DEFAULT_POLICY: dict[SafetyCategory, CategoryPolicy] = {
    SafetyCategory.SAFE: CategoryPolicy(Severity.CRITICAL, Severity.CRITICAL, Severity.CRITICAL),
    # Zero-tolerance floor — never relaxable, never softenable.
    SafetyCategory.SEXUAL_MINORS: CategoryPolicy(
        Severity.LOW, Severity.LOW, Severity.LOW, zero_tolerance=True
    ),
    SafetyCategory.EXTREMISM: CategoryPolicy(
        Severity.LOW, Severity.LOW, Severity.LOW, zero_tolerance=True
    ),
    # Literary categories — soften first.
    SafetyCategory.VIOLENCE: CategoryPolicy(
        Severity.LOW, Severity.HIGH, Severity.CRITICAL, softenable=True
    ),
    SafetyCategory.GORE: CategoryPolicy(
        Severity.LOW, Severity.MEDIUM, Severity.HIGH, softenable=True
    ),
    SafetyCategory.SEXUAL: CategoryPolicy(
        Severity.LOW, Severity.MEDIUM, Severity.HIGH, softenable=True
    ),
    SafetyCategory.NUDITY: CategoryPolicy(
        Severity.LOW, Severity.MEDIUM, Severity.HIGH, softenable=True
    ),
    # Non-softenable — held / blocked, never rewritten.
    SafetyCategory.MINORS: CategoryPolicy(Severity.MEDIUM, Severity.MEDIUM, Severity.HIGH),
    SafetyCategory.HATE: CategoryPolicy(Severity.LOW, Severity.MEDIUM, Severity.HIGH),
    SafetyCategory.HARASSMENT: CategoryPolicy(Severity.MEDIUM, Severity.HIGH, Severity.CRITICAL),
    SafetyCategory.SELF_HARM: CategoryPolicy(Severity.MEDIUM, Severity.MEDIUM, Severity.HIGH),
    SafetyCategory.WEAPONS: CategoryPolicy(Severity.MEDIUM, Severity.HIGH, Severity.CRITICAL),
    SafetyCategory.DRUGS: CategoryPolicy(Severity.MEDIUM, Severity.HIGH, Severity.CRITICAL),
    SafetyCategory.PROFANITY: CategoryPolicy(
        Severity.MEDIUM, Severity.CRITICAL, Severity.CRITICAL, softenable=True
    ),
    # Advisory-leaning categories — rarely block, mostly tag.
    SafetyCategory.SUBSTANCE: CategoryPolicy(Severity.HIGH, Severity.CRITICAL, Severity.CRITICAL),
    SafetyCategory.FRIGHTENING: CategoryPolicy(
        Severity.HIGH, Severity.CRITICAL, Severity.CRITICAL
    ),
    SafetyCategory.OTHER: CategoryPolicy(Severity.MEDIUM, Severity.HIGH, Severity.CRITICAL),
}


def default_policy(category: SafetyCategory) -> CategoryPolicy:
    """The baseline :class:`CategoryPolicy` for ``category`` (OTHER as fallback)."""
    return DEFAULT_POLICY.get(category, DEFAULT_POLICY[SafetyCategory.OTHER])


#: Categories that can never be allowed / softened / overridden away — the gateway
#: refuses them even under a director "evolve canon" override.
ZERO_TOLERANCE_CATEGORIES: frozenset[SafetyCategory] = frozenset(
    cat for cat, pol in DEFAULT_POLICY.items() if pol.zero_tolerance
)


def is_softenable(category: SafetyCategory) -> bool:
    """True when the softener is allowed to attempt an intent-preserving rewrite."""
    return default_policy(category).softenable


__all__ = [
    "DEFAULT_POLICY",
    "ZERO_TOLERANCE_CATEGORIES",
    "CategoryPolicy",
    "SafetyAction",
    "SafetyCategory",
    "Severity",
    "default_policy",
    "is_softenable",
]
