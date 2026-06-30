"""Age-rating / content-advisory tagger for a whole book's adaptation.

Before (or during) ingest, the gateway can tag a book with a film-classification
style advisory — an :class:`~app.safety.contracts.AgeRating` band plus the
human-facing descriptors ("violence", "brief nudity", "frightening imagery") and
the worst severity observed per category. The reading-room UI surfaces this as an
advisory card, and the gateway can use it to pick a default per-book policy
strictness.

Pure aggregation over classifier findings (one pass per chapter / span), no I/O.
The rating is the **strictest band any span reached**: a single CRITICAL-gore
chapter pulls a whole otherwise-gentle book up to R/NC-17, exactly as a film
classification works.
"""

from __future__ import annotations

from app.safety.contracts import AgeRating, ContentAdvisory, Finding
from app.safety.taxonomy import SafetyCategory, Severity

#: Per-category, per-severity contribution to the age band. The strictest band any
#: category reaches wins. Categories absent from this map (e.g. SAFE, OTHER) do not
#: drive the rating. Zero-tolerance categories at any positive severity force the
#: top band (the book would be rejected at ingest, but the advisory still reflects
#: the worst the gateway saw).
_RATING_TABLE: dict[SafetyCategory, dict[Severity, AgeRating]] = {
    SafetyCategory.SEXUAL_MINORS: {
        Severity.LOW: AgeRating.NC17,
        Severity.MEDIUM: AgeRating.NC17,
        Severity.HIGH: AgeRating.NC17,
        Severity.CRITICAL: AgeRating.NC17,
    },
    SafetyCategory.EXTREMISM: {
        Severity.LOW: AgeRating.NC17,
        Severity.MEDIUM: AgeRating.NC17,
        Severity.HIGH: AgeRating.NC17,
        Severity.CRITICAL: AgeRating.NC17,
    },
    SafetyCategory.SEXUAL: {
        Severity.LOW: AgeRating.PG13,
        Severity.MEDIUM: AgeRating.R,
        Severity.HIGH: AgeRating.NC17,
        Severity.CRITICAL: AgeRating.NC17,
    },
    SafetyCategory.NUDITY: {
        Severity.LOW: AgeRating.PG13,
        Severity.MEDIUM: AgeRating.R,
        Severity.HIGH: AgeRating.NC17,
        Severity.CRITICAL: AgeRating.NC17,
    },
    SafetyCategory.VIOLENCE: {
        Severity.LOW: AgeRating.PG,
        Severity.MEDIUM: AgeRating.PG13,
        Severity.HIGH: AgeRating.R,
        Severity.CRITICAL: AgeRating.R,
    },
    SafetyCategory.GORE: {
        Severity.LOW: AgeRating.PG13,
        Severity.MEDIUM: AgeRating.R,
        Severity.HIGH: AgeRating.R,
        Severity.CRITICAL: AgeRating.NC17,
    },
    SafetyCategory.HATE: {
        Severity.LOW: AgeRating.PG13,
        Severity.MEDIUM: AgeRating.R,
        Severity.HIGH: AgeRating.R,
        Severity.CRITICAL: AgeRating.R,
    },
    SafetyCategory.HARASSMENT: {
        Severity.LOW: AgeRating.PG13,
        Severity.MEDIUM: AgeRating.PG13,
        Severity.HIGH: AgeRating.R,
        Severity.CRITICAL: AgeRating.R,
    },
    SafetyCategory.SELF_HARM: {
        Severity.LOW: AgeRating.PG13,
        Severity.MEDIUM: AgeRating.R,
        Severity.HIGH: AgeRating.R,
        Severity.CRITICAL: AgeRating.R,
    },
    SafetyCategory.DRUGS: {
        Severity.LOW: AgeRating.PG13,
        Severity.MEDIUM: AgeRating.R,
        Severity.HIGH: AgeRating.R,
        Severity.CRITICAL: AgeRating.R,
    },
    SafetyCategory.SUBSTANCE: {
        Severity.LOW: AgeRating.PG,
        Severity.MEDIUM: AgeRating.PG13,
        Severity.HIGH: AgeRating.R,
        Severity.CRITICAL: AgeRating.R,
    },
    SafetyCategory.WEAPONS: {
        Severity.LOW: AgeRating.PG13,
        Severity.MEDIUM: AgeRating.R,
        Severity.HIGH: AgeRating.R,
        Severity.CRITICAL: AgeRating.R,
    },
    SafetyCategory.PROFANITY: {
        Severity.LOW: AgeRating.PG,
        Severity.MEDIUM: AgeRating.PG13,
        Severity.HIGH: AgeRating.R,
        Severity.CRITICAL: AgeRating.R,
    },
    SafetyCategory.FRIGHTENING: {
        Severity.LOW: AgeRating.PG,
        Severity.MEDIUM: AgeRating.PG13,
        Severity.HIGH: AgeRating.PG13,
        Severity.CRITICAL: AgeRating.R,
    },
    SafetyCategory.MINORS: {
        Severity.LOW: AgeRating.PG,
        Severity.MEDIUM: AgeRating.PG13,
        Severity.HIGH: AgeRating.R,
        Severity.CRITICAL: AgeRating.R,
    },
}

#: Human-facing descriptor phrasing per category (film-classification style).
_DESCRIPTORS: dict[SafetyCategory, str] = {
    SafetyCategory.SEXUAL: "sexual content",
    SafetyCategory.NUDITY: "nudity",
    SafetyCategory.VIOLENCE: "violence",
    SafetyCategory.GORE: "graphic / bloody imagery",
    SafetyCategory.HATE: "discriminatory language",
    SafetyCategory.HARASSMENT: "menacing behaviour",
    SafetyCategory.SELF_HARM: "depictions of self-harm",
    SafetyCategory.DRUGS: "drug references",
    SafetyCategory.SUBSTANCE: "alcohol / drug use",
    SafetyCategory.WEAPONS: "weapons",
    SafetyCategory.PROFANITY: "strong language",
    SafetyCategory.FRIGHTENING: "frightening / intense scenes",
    SafetyCategory.MINORS: "minors in peril",
    SafetyCategory.SEXUAL_MINORS: "child sexual content",
    SafetyCategory.EXTREMISM: "violent extremism",
}

#: Severity-prefix so the descriptor reads like a real advisory ("brief", "strong").
_SEVERITY_PREFIX: dict[Severity, str] = {
    Severity.LOW: "mild",
    Severity.MEDIUM: "moderate",
    Severity.HIGH: "strong",
    Severity.CRITICAL: "extreme",
}


def rate_findings(findings: list[Finding]) -> ContentAdvisory:
    """Aggregate a flat list of findings into one :class:`ContentAdvisory`.

    Use this when you already have the findings for a whole book (e.g. one
    classifier pass per chapter, concatenated). For incremental tagging across
    spans, accumulate with :class:`AdvisoryAccumulator`.
    """
    worst_by_cat: dict[SafetyCategory, Severity] = {}
    for f in findings:
        if not f.positive:
            continue
        cur = worst_by_cat.get(f.category, Severity.NONE)
        if f.severity > cur:
            worst_by_cat[f.category] = f.severity
    return _build_advisory(worst_by_cat)


class AdvisoryAccumulator:
    """Accumulate the worst severity per category across many spans/chapters.

    Streaming-friendly: feed each span's findings, then call :meth:`result`. Useful
    during ingest where chapters are classified one at a time and a whole-book pass
    is never materialised in memory.
    """

    def __init__(self) -> None:
        self._worst: dict[SafetyCategory, Severity] = {}

    def add(self, findings: list[Finding]) -> None:
        for f in findings:
            if not f.positive:
                continue
            cur = self._worst.get(f.category, Severity.NONE)
            if f.severity > cur:
                self._worst[f.category] = f.severity

    def result(self) -> ContentAdvisory:
        return _build_advisory(dict(self._worst))


def _build_advisory(worst_by_cat: dict[SafetyCategory, Severity]) -> ContentAdvisory:
    ratings: list[AgeRating] = []
    descriptors: list[str] = []
    for cat, sev in sorted(worst_by_cat.items(), key=lambda kv: (-int(kv[1]), kv[0].value)):
        table = _RATING_TABLE.get(cat)
        if table is not None:
            ratings.append(table.get(sev, AgeRating.R))
        phrase = _DESCRIPTORS.get(cat)
        if phrase is not None:
            prefix = _SEVERITY_PREFIX.get(sev, "")
            descriptors.append(f"{prefix} {phrase}".strip())

    rating = AgeRating.strictest(ratings)
    if not worst_by_cat:
        rationale = "no flagged content detected"
    else:
        rationale = "rated " + rating.value + " for " + (
            "; ".join(descriptors) if descriptors else "flagged content"
        )
    return ContentAdvisory(
        rating=rating,
        descriptors=descriptors,
        category_severity=dict(worst_by_cat),
        rationale=rationale,
    )


__all__ = [
    "AdvisoryAccumulator",
    "rate_findings",
]
