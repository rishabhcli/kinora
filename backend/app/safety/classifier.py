"""The pluggable safety-classifier seam (text + sampled frames).

Every model-based judgment in the gateway flows through the :class:`Protocol`\\ s
here. Production wires a model-backed implementation that calls the shared chat /
VL providers; **tests inject a deterministic fake** (:class:`KeywordSafetyClassifier`)
so the whole gateway — rule engine, softener, prompt/output gates, routing,
decision log — is exercised with **zero live model calls and zero credits**.

This mirrors the project's seam discipline (the §5.4 comment classifier, the
Critic's embedder, the :mod:`app.moderation` classifier): the contract is the
Protocol, the concrete impl is swapped at the composition root, and the fake is a
faithful keyword/regex router that can drive **every** taxonomy category.

Design notes
------------
* A classifier **always** returns ≥1 :class:`Finding`; when nothing fired it
  returns a single ``SAFE`` finding so downstream code never special-cases empty.
* On a model error the classifier returns a ``degraded`` result rather than
  raising, so a provider blip can never crash a render. The *gate* decides how
  strict to be about a degraded result.
* The fake's keyword map is the same shape as the moderation fake so a reviewer
  reading both subsystems sees one consistent vocabulary.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from app.core.logging import get_logger
from app.safety.contracts import Finding, PromptAssessment, SafetySurface
from app.safety.taxonomy import SafetyCategory

if TYPE_CHECKING:
    from app.core.config import Settings
    from app.providers import Providers

logger = get_logger("app.safety.classifier")


@runtime_checkable
class TextSafetyClassifier(Protocol):
    """Classifies a piece of text (a prompt or book span) against the taxonomy."""

    async def classify_text(self, text: str, *, surface: SafetySurface) -> PromptAssessment:
        """Return the findings fired for ``text`` (≥1 finding; SAFE when clean)."""
        ...


@runtime_checkable
class FrameSafetyClassifier(Protocol):
    """Classifies sampled image frames (a keyframe or sampled clip frames)."""

    async def classify_frames(
        self, frames: list[bytes], *, surface: SafetySurface
    ) -> PromptAssessment:
        """Return the findings fired across ``frames`` (≥1 finding; SAFE clean)."""
        ...


@runtime_checkable
class SafetyClassifier(TextSafetyClassifier, FrameSafetyClassifier, Protocol):
    """A classifier covering both modalities — what the gateway depends on."""


# --------------------------------------------------------------------------- #
# Deterministic fake (the test/offline default) — no network, no spend.
# --------------------------------------------------------------------------- #

#: Keyword → (category, score) map. Scores land each term in a sensible severity
#: tier via taxonomy.Severity.from_score: CRITICAL ≥ 0.85, HIGH 0.6–0.85,
#: MEDIUM 0.4–0.6, LOW 0.2–0.4. Longest matches win so "child sexual" beats "child".
_KEYWORD_RULES: tuple[tuple[str, SafetyCategory, float], ...] = (
    # Zero-tolerance — any hit blocks regardless of score.
    ("csam", SafetyCategory.SEXUAL_MINORS, 0.99),
    ("child sexual", SafetyCategory.SEXUAL_MINORS, 0.99),
    ("sexual abuse of a child", SafetyCategory.SEXUAL_MINORS, 0.99),
    ("terror attack plan", SafetyCategory.EXTREMISM, 0.97),
    ("build a bomb", SafetyCategory.EXTREMISM, 0.95),
    ("recruit for jihad", SafetyCategory.EXTREMISM, 0.92),
    # Sexual
    ("explicit sex", SafetyCategory.SEXUAL, 0.9),
    ("pornographic", SafetyCategory.SEXUAL, 0.88),
    ("graphic sex scene", SafetyCategory.SEXUAL, 0.8),
    ("sexual", SafetyCategory.SEXUAL, 0.55),
    # Nudity (often distinct from sexual in provider policies)
    ("fully nude", SafetyCategory.NUDITY, 0.7),
    ("nude", SafetyCategory.NUDITY, 0.5),
    ("naked", SafetyCategory.NUDITY, 0.45),
    # Minors (non-sexual sensitive)
    ("child", SafetyCategory.MINORS, 0.45),
    ("minor", SafetyCategory.MINORS, 0.4),
    # Violence / gore
    ("graphic gore", SafetyCategory.GORE, 0.9),
    ("dismember", SafetyCategory.GORE, 0.88),
    ("disembowel", SafetyCategory.GORE, 0.88),
    ("blood everywhere", SafetyCategory.GORE, 0.7),
    ("bloody", SafetyCategory.GORE, 0.45),
    ("massacre", SafetyCategory.VIOLENCE, 0.9),
    ("brutal beating", SafetyCategory.VIOLENCE, 0.7),
    ("graphic stabbing", SafetyCategory.VIOLENCE, 0.7),
    ("stab", SafetyCategory.VIOLENCE, 0.6),
    ("kill", SafetyCategory.VIOLENCE, 0.5),
    ("fight", SafetyCategory.VIOLENCE, 0.3),
    # Hate / harassment (never softenable)
    ("hate speech", SafetyCategory.HATE, 0.9),
    ("racial slur", SafetyCategory.HATE, 0.92),
    ("ethnic cleansing", SafetyCategory.HATE, 0.95),
    ("you should die", SafetyCategory.HARASSMENT, 0.8),
    # Self-harm
    ("how to commit suicide", SafetyCategory.SELF_HARM, 0.92),
    ("self-harm", SafetyCategory.SELF_HARM, 0.7),
    ("self harm", SafetyCategory.SELF_HARM, 0.7),
    # Weapons / drugs
    ("ghost gun blueprint", SafetyCategory.WEAPONS, 0.9),
    ("synthesize methamphetamine", SafetyCategory.DRUGS, 0.9),
    ("smoking opium", SafetyCategory.SUBSTANCE, 0.5),
    ("drunk", SafetyCategory.SUBSTANCE, 0.35),
    # Frightening (advisory)
    ("terrifying monster", SafetyCategory.FRIGHTENING, 0.5),
    ("horror", SafetyCategory.FRIGHTENING, 0.4),
    # Profanity
    ("damn", SafetyCategory.PROFANITY, 0.3),
)

#: Regexes that mine sensitive text deterministically (no model needed).
_SSN_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")


class KeywordSafetyClassifier:
    """Deterministic keyword/regex classifier — the offline + test default.

    Faithful enough to drive **every** taxonomy category so tests assert real
    routing, and a serviceable cheap pre-filter in production (an obvious hit is
    decided before a single model token is spent). The frame path keys off
    injected ``frame_findings`` (a test handle) and otherwise returns SAFE — a real
    deployment swaps in :class:`ModelFrameSafetyClassifier`.
    """

    name = "keyword"

    def __init__(
        self, *, frame_findings: dict[bytes, list[Finding]] | None = None
    ) -> None:
        self._frame_findings = frame_findings or {}

    async def classify_text(self, text: str, *, surface: SafetySurface) -> PromptAssessment:
        findings = self.scan_text(text)
        return PromptAssessment(
            surface=surface,
            findings=findings or [Finding.of(SafetyCategory.SAFE, 0.0, source="fake")],
            classifier=self.name,
        )

    async def classify_frames(
        self, frames: list[bytes], *, surface: SafetySurface
    ) -> PromptAssessment:
        findings: list[Finding] = []
        for fr in frames:
            findings.extend(self._frame_findings.get(fr, []))
        return PromptAssessment(
            surface=surface,
            findings=findings or [Finding.of(SafetyCategory.SAFE, 0.0, source="fake")],
            classifier=self.name,
        )

    @staticmethod
    def scan_text(text: str) -> list[Finding]:
        """Pure scan: the rule engine reuses this so prompt routing is deterministic.

        Keeps the highest score per category so a phrase that matches both a broad
        ("kill") and a specific ("massacre") term reports the worse one once.
        """
        lowered = text.lower()
        best: dict[SafetyCategory, Finding] = {}
        for term, category, score in _KEYWORD_RULES:
            if term in lowered:
                cur = best.get(category)
                if cur is None or score > cur.score:
                    best[category] = Finding.of(
                        category, score, rationale=f"matched {term!r}", source="rule"
                    )
        if _SSN_RE.search(text):
            best.setdefault(
                SafetyCategory.OTHER,
                Finding.of(SafetyCategory.OTHER, 0.6, rationale="SSN pattern", source="rule"),
            )
        return list(best.values())


# --------------------------------------------------------------------------- #
# Model-backed implementations (production) — NEVER invoked by the unit suite.
# --------------------------------------------------------------------------- #

_MODEL_INSTRUCTION = (
    "You are a content-safety classifier for a book-to-film generation pipeline. "
    "Read the input and return ONLY JSON: "
    '{"findings": [{"category": <one taxonomy category>, "score": <0..1>, '
    '"rationale": <short string>}]}. Use the taxonomy categories exactly. If the '
    'content is clean, return {"findings": [{"category": "safe", "score": 0.0}]}. '
    "Score is your confidence the category applies AND how severe it is."
)

_VALID_CATEGORIES = {c.value for c in SafetyCategory}


def _findings_from_model(raw: object) -> list[Finding]:
    """Project a model's ``{"findings": [...]}`` reply into typed findings (defensive)."""
    findings: list[Finding] = []
    items = raw.get("findings", []) if isinstance(raw, dict) else []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        cat = str(item.get("category", "")).lower()
        if cat not in _VALID_CATEGORIES:
            cat = SafetyCategory.OTHER.value
        try:
            score = float(item.get("score", 0.0))
        except (TypeError, ValueError):
            score = 0.0
        rationale = item.get("rationale")
        findings.append(
            Finding.of(
                SafetyCategory(cat),
                max(0.0, min(1.0, score)),
                rationale=str(rationale) if rationale is not None else None,
                source="model",
            )
        )
    return findings


def _merge_findings(a: list[Finding], b: list[Finding]) -> list[Finding]:
    """Merge two finding lists, keeping the highest score per category."""
    best: dict[SafetyCategory, Finding] = {}
    for f in [*a, *b]:
        cur = best.get(f.category)
        if cur is None or f.score > cur.score:
            best[f.category] = f
    return list(best.values())


def _sample(frames: list[bytes], k: int) -> list[bytes]:
    """Evenly sample at most ``k`` frames (first + spread) for a bounded request."""
    if len(frames) <= k:
        return list(frames)
    step = len(frames) / k
    return [frames[int(i * step)] for i in range(k)]


class ModelTextSafetyClassifier:
    """Text classifier backed by the shared chat provider (production lane).

    Pairs a cheap keyword pre-filter with a model call so an obvious zero-tolerance
    hit short-circuits without spend; the pre-filter's findings are merged with the
    model's so the model can never *hide* a floor term. A failure returns a
    ``degraded`` (pre-filter-only) result — never raises.
    """

    name = "model_text"

    def __init__(self, providers: Providers, *, settings: Settings) -> None:
        self._providers = providers
        self._settings = settings

    async def classify_text(self, text: str, *, surface: SafetySurface) -> PromptAssessment:
        prefilter = KeywordSafetyClassifier.scan_text(text)
        try:
            raw = await self._providers.chat.chat_json(
                [
                    {"role": "system", "content": _MODEL_INSTRUCTION},
                    {"role": "user", "content": text[:8000]},
                ],
                self._settings.chat_model_adapter,
                temperature=0.0,
            )
            model_findings = _findings_from_model(raw)
        except Exception as exc:  # noqa: BLE001 - a provider blip must never crash a gate
            logger.warning("safety.text_classifier.degraded", error=str(exc))
            return PromptAssessment(
                surface=surface,
                findings=prefilter or [Finding.of(SafetyCategory.SAFE, 0.0, source="model")],
                classifier=self.name,
                degraded=True,
            )
        merged = _merge_findings(prefilter, model_findings)
        return PromptAssessment(
            surface=surface,
            findings=merged or [Finding.of(SafetyCategory.SAFE, 0.0, source="model")],
            classifier=self.name,
        )


class ModelFrameSafetyClassifier:
    """Frame classifier backed by the shared VL provider (production lane).

    Samples the supplied frames and asks the VL model for taxonomy findings. A
    failure returns a ``degraded`` SAFE result (never raises). Frame sampling is
    bounded so a long clip's frame list never balloons the request.
    """

    name = "model_frame"
    max_frames = 4

    def __init__(self, providers: Providers, *, settings: Settings) -> None:
        self._providers = providers
        self._settings = settings

    async def classify_frames(
        self, frames: list[bytes], *, surface: SafetySurface
    ) -> PromptAssessment:
        if not frames:
            return PromptAssessment(
                surface=surface,
                findings=[Finding.of(SafetyCategory.SAFE, 0.0, source="model")],
                classifier=self.name,
            )
        sampled: list[bytes | str] = list(_sample(frames, self.max_frames))
        try:
            raw = await self._providers.vl.analyze_json(
                sampled,
                _MODEL_INSTRUCTION,
                model=self._settings.vl_model,
                temperature=0.0,
            )
            findings = _findings_from_model(raw)
        except Exception as exc:  # noqa: BLE001 - a provider blip must never crash a gate
            logger.warning("safety.frame_classifier.degraded", error=str(exc))
            return PromptAssessment(
                surface=surface,
                findings=[Finding.of(SafetyCategory.SAFE, 0.0, source="model")],
                classifier=self.name,
                degraded=True,
            )
        return PromptAssessment(
            surface=surface,
            findings=findings or [Finding.of(SafetyCategory.SAFE, 0.0, source="model")],
            classifier=self.name,
        )


class CompositeSafetyClassifier:
    """Glue a text classifier and a frame classifier into one :class:`SafetyClassifier`."""

    def __init__(
        self,
        *,
        text: TextSafetyClassifier,
        frames: FrameSafetyClassifier,
        name: str,
    ) -> None:
        self._text = text
        self._frames = frames
        self.name = name

    async def classify_text(self, text: str, *, surface: SafetySurface) -> PromptAssessment:
        return await self._text.classify_text(text, surface=surface)

    async def classify_frames(
        self, frames: list[bytes], *, surface: SafetySurface
    ) -> PromptAssessment:
        return await self._frames.classify_frames(frames, surface=surface)


def build_default_classifier(
    providers: Providers | None = None, *, settings: Settings | None = None
) -> SafetyClassifier:
    """Build the production classifier, or the offline keyword fake when no providers.

    The composition root passes the wired providers; without them (or in tests) the
    keyword classifier is returned, so importing this module never forces a
    provider/network dependency.
    """
    if providers is None or settings is None:
        return KeywordSafetyClassifier()
    return CompositeSafetyClassifier(
        text=ModelTextSafetyClassifier(providers, settings=settings),
        frames=ModelFrameSafetyClassifier(providers, settings=settings),
        name="model_composite",
    )


__all__ = [
    "CompositeSafetyClassifier",
    "FrameSafetyClassifier",
    "KeywordSafetyClassifier",
    "ModelFrameSafetyClassifier",
    "ModelTextSafetyClassifier",
    "SafetyClassifier",
    "TextSafetyClassifier",
    "build_default_classifier",
]
