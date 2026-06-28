"""The moderation policy taxonomy — categories, severity tiers, dispositions.

This is the **vocabulary** every other layer speaks. It is intentionally small,
ordered, and free of any I/O so it can be imported anywhere (including the DB
models and the API schemas) without pulling in services.

Three orthogonal axes:

* :class:`ModerationCategory` — *what kind* of policy concern a label is about
  (sexual content, minors, violence/gore, hate, self-harm, …). These mirror the
  industry-standard safety taxonomies so a real model classifier's labels map
  onto them cleanly.
* :class:`Severity` — *how bad* a given label is, on an ordered 0–4 scale. A
  category alone is not enough: "violence" at ``LOW`` (a cartoon scuffle) and at
  ``CRITICAL`` (graphic gore) get very different dispositions.
* :class:`Disposition` — *what to do*: ``ALLOW`` / ``FLAG`` / ``BLOCK``. The
  policy engine resolves a bag of labels into exactly one of these.

:data:`DEFAULT_DISPOSITIONS` is the baseline policy: for each category, the
``Severity`` threshold at which a label flips from FLAG to BLOCK, and whether the
category is *zero-tolerance* (any positive label blocks regardless of severity).
A per-tenant policy (:mod:`.tenant_policy`) overrides this map; the defaults are
deliberately conservative.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass


class ModerationCategory(enum.StrEnum):
    """A policy concern a classifier label can be *about* (§10 guardrails).

    Mirrors the common safety-taxonomy families so a model classifier's native
    labels map on cleanly. ``SAFE`` is the explicit "nothing flagged" label so a
    classifier always returns at least one label.
    """

    SAFE = "safe"
    SEXUAL = "sexual"
    #: Sexual content involving (or sexualising) minors — always zero-tolerance.
    SEXUAL_MINORS = "sexual_minors"
    MINORS = "minors"  # depiction of minors in a non-sexual but sensitive context
    VIOLENCE = "violence"
    GORE = "gore"
    HATE = "hate"
    HARASSMENT = "harassment"
    SELF_HARM = "self_harm"
    EXTREMISM = "extremism"  # terrorism / violent extremist content
    WEAPONS = "weapons"  # instructional weapon-making
    DRUGS = "drugs"
    PROFANITY = "profanity"
    #: Personally-identifying information surfaced in generated/source content.
    PII = "pii"
    #: Copyright / IP concern flagged on the *source* book at ingest.
    COPYRIGHT = "copyright"
    SPAM = "spam"
    OTHER = "other"


class Severity(enum.IntEnum):
    """Ordered severity of a single label (higher = worse).

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
        ``[0.6,0.85)→HIGH``, ``[0.85,1]→CRITICAL``. Clamped to ``[0,1]``.
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


class Disposition(enum.StrEnum):
    """What the policy engine decides to *do* with content.

    Ordered by strictness via :attr:`rank` so the engine can take the strictest
    disposition across many labels.
    """

    ALLOW = "allow"
    FLAG = "flag"  # surfaced to human review; may still be shown, depending on gate
    BLOCK = "block"  # never shown / never ingested

    @property
    def rank(self) -> int:
        """Strictness rank (BLOCK > FLAG > ALLOW) for taking the max."""
        return {Disposition.ALLOW: 0, Disposition.FLAG: 1, Disposition.BLOCK: 2}[self]

    @classmethod
    def strictest(cls, dispositions: list[Disposition]) -> Disposition:
        """The strictest disposition in the list (ALLOW if empty)."""
        return max(dispositions, default=cls.ALLOW, key=lambda d: d.rank)


@dataclass(frozen=True, slots=True)
class CategoryRule:
    """The default policy for one category.

    Args:
        flag_at: severity at/above which a label is at least FLAGged.
        block_at: severity at/above which a label is BLOCKed.
        zero_tolerance: if True, *any* positive label (severity > NONE) BLOCKs,
            regardless of ``block_at`` (used for CSAM / extremism).
    """

    flag_at: Severity
    block_at: Severity
    zero_tolerance: bool = False

    def disposition_for(self, severity: Severity) -> Disposition:
        """Resolve this category's disposition for a label at ``severity``."""
        if severity <= Severity.NONE:
            return Disposition.ALLOW
        if self.zero_tolerance:
            return Disposition.BLOCK
        if severity >= self.block_at:
            return Disposition.BLOCK
        if severity >= self.flag_at:
            return Disposition.FLAG
        return Disposition.ALLOW


#: The conservative baseline policy. Zero-tolerance categories block on any
#: positive label; everything else flags at MEDIUM and blocks at HIGH/CRITICAL.
DEFAULT_DISPOSITIONS: dict[ModerationCategory, CategoryRule] = {
    ModerationCategory.SAFE: CategoryRule(Severity.CRITICAL, Severity.CRITICAL),
    ModerationCategory.SEXUAL_MINORS: CategoryRule(
        Severity.LOW, Severity.LOW, zero_tolerance=True
    ),
    ModerationCategory.EXTREMISM: CategoryRule(Severity.LOW, Severity.LOW, zero_tolerance=True),
    ModerationCategory.SEXUAL: CategoryRule(Severity.MEDIUM, Severity.HIGH),
    ModerationCategory.MINORS: CategoryRule(Severity.MEDIUM, Severity.HIGH),
    ModerationCategory.VIOLENCE: CategoryRule(Severity.HIGH, Severity.CRITICAL),
    ModerationCategory.GORE: CategoryRule(Severity.MEDIUM, Severity.HIGH),
    ModerationCategory.HATE: CategoryRule(Severity.MEDIUM, Severity.HIGH),
    ModerationCategory.HARASSMENT: CategoryRule(Severity.HIGH, Severity.CRITICAL),
    ModerationCategory.SELF_HARM: CategoryRule(Severity.MEDIUM, Severity.HIGH),
    ModerationCategory.WEAPONS: CategoryRule(Severity.HIGH, Severity.CRITICAL),
    ModerationCategory.DRUGS: CategoryRule(Severity.HIGH, Severity.CRITICAL),
    ModerationCategory.PROFANITY: CategoryRule(Severity.CRITICAL, Severity.CRITICAL),
    ModerationCategory.PII: CategoryRule(Severity.MEDIUM, Severity.HIGH),
    ModerationCategory.COPYRIGHT: CategoryRule(Severity.HIGH, Severity.CRITICAL),
    ModerationCategory.SPAM: CategoryRule(Severity.HIGH, Severity.CRITICAL),
    ModerationCategory.OTHER: CategoryRule(Severity.HIGH, Severity.CRITICAL),
}


def default_rule(category: ModerationCategory) -> CategoryRule:
    """The baseline :class:`CategoryRule` for ``category`` (OTHER's rule as fallback)."""
    return DEFAULT_DISPOSITIONS.get(category, DEFAULT_DISPOSITIONS[ModerationCategory.OTHER])


#: Categories that can never be allowed/evolved away (used by the gate to refuse
#: even a director "evolve canon" override).
ZERO_TOLERANCE_CATEGORIES: frozenset[ModerationCategory] = frozenset(
    cat for cat, rule in DEFAULT_DISPOSITIONS.items() if rule.zero_tolerance
)


__all__ = [
    "DEFAULT_DISPOSITIONS",
    "ZERO_TOLERANCE_CATEGORIES",
    "CategoryRule",
    "Disposition",
    "ModerationCategory",
    "Severity",
    "default_rule",
]
