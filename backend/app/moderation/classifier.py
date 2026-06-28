"""The pluggable content-classifier seam (text + image/video frame), §9, §10.

Every model-based judgment in the moderation subsystem flows through the two
:class:`Protocol`\\ s defined here. Production wires a model-backed implementation
(:class:`ModelTextClassifier` / :class:`ModelVisionClassifier`) that calls the
shared providers; **tests inject a deterministic fake** (:class:`KeywordClassifier`)
so the entire subsystem — gates, policy, review, escalation, audit — is exercised
with **zero live model calls and zero credits**.

This mirrors the project's existing seam discipline (the §5.4 comment classifier,
the Critic's embedder): the contract is the Protocol, the concrete impl is swapped
at the composition root, and the fake is a faithful keyword router.

Design notes
------------
* A classifier **always** returns at least one :class:`ContentLabel`; when nothing
  fired it returns a single ``SAFE`` label. Downstream code never special-cases an
  empty list.
* On a model error the classifier returns a ``degraded`` result rather than raising,
  so a transient provider blip can never crash a render or an ingest. The *gate*
  decides how strict to be about a degraded result (fail-open vs fail-closed),
  configured per-surface — a degraded *generation* result fails open (the Critic +
  later passes still run), a degraded *ingest* result fails closed by default.
* The fake's keyword map is exhaustive enough to drive every taxonomy category, so
  policy/gate tests can assert real category routing without a model.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from app.core.logging import get_logger
from app.moderation.contracts import (
    ClassificationResult,
    ContentLabel,
    Surface,
)
from app.moderation.taxonomy import ModerationCategory

if TYPE_CHECKING:
    from app.core.config import Settings
    from app.providers import Providers

logger = get_logger("app.moderation.classifier")


@runtime_checkable
class TextClassifier(Protocol):
    """Classifies a piece of text against the moderation taxonomy."""

    async def classify_text(self, text: str, *, surface: Surface) -> ClassificationResult:
        """Return the labels fired for ``text`` (≥1 label; SAFE when clean)."""
        ...


@runtime_checkable
class VisionClassifier(Protocol):
    """Classifies one or more image frames (a keyframe or sampled clip frames)."""

    async def classify_frames(
        self, frames: list[bytes], *, surface: Surface
    ) -> ClassificationResult:
        """Return the labels fired across ``frames`` (≥1 label; SAFE when clean)."""
        ...


@runtime_checkable
class ContentClassifier(TextClassifier, VisionClassifier, Protocol):
    """A classifier covering both modalities — what the gate depends on."""


# --------------------------------------------------------------------------- #
# Deterministic fake (the test/offline default) — no network, no spend.
# --------------------------------------------------------------------------- #

#: Keyword → (category, score) map. Scores are chosen so the default severity
#: bucketing (taxonomy.Severity.from_score) lands each term in a sensible tier:
#:   CRITICAL terms ≥ 0.85, HIGH 0.6–0.85, MEDIUM 0.4–0.6, LOW 0.2–0.4.
_KEYWORD_RULES: tuple[tuple[str, ModerationCategory, float], ...] = (
    # Zero-tolerance — any hit blocks regardless of score, but score still recorded.
    ("csam", ModerationCategory.SEXUAL_MINORS, 0.99),
    ("child sexual", ModerationCategory.SEXUAL_MINORS, 0.99),
    ("terror attack plan", ModerationCategory.EXTREMISM, 0.97),
    ("build a bomb", ModerationCategory.EXTREMISM, 0.95),
    ("recruit for jihad", ModerationCategory.EXTREMISM, 0.92),
    # Sexual
    ("explicit sex", ModerationCategory.SEXUAL, 0.9),
    ("pornographic", ModerationCategory.SEXUAL, 0.88),
    ("nude", ModerationCategory.SEXUAL, 0.5),
    ("sexual", ModerationCategory.SEXUAL, 0.55),
    # Minors (non-sexual sensitive)
    ("child", ModerationCategory.MINORS, 0.45),
    ("minor", ModerationCategory.MINORS, 0.45),
    # Violence / gore
    ("graphic gore", ModerationCategory.GORE, 0.9),
    ("dismember", ModerationCategory.GORE, 0.88),
    ("blood everywhere", ModerationCategory.GORE, 0.7),
    ("kill", ModerationCategory.VIOLENCE, 0.5),
    ("stab", ModerationCategory.VIOLENCE, 0.6),
    ("massacre", ModerationCategory.VIOLENCE, 0.9),
    # Hate / harassment
    ("hate speech", ModerationCategory.HATE, 0.9),
    ("racial slur", ModerationCategory.HATE, 0.92),
    ("ethnic cleansing", ModerationCategory.HATE, 0.95),
    ("you should die", ModerationCategory.HARASSMENT, 0.8),
    # Self-harm
    ("how to commit suicide", ModerationCategory.SELF_HARM, 0.92),
    ("self-harm", ModerationCategory.SELF_HARM, 0.7),
    ("self harm", ModerationCategory.SELF_HARM, 0.7),
    # Weapons / drugs
    ("ghost gun blueprint", ModerationCategory.WEAPONS, 0.9),
    ("synthesize methamphetamine", ModerationCategory.DRUGS, 0.9),
    # PII
    ("ssn", ModerationCategory.PII, 0.6),
    ("social security number", ModerationCategory.PII, 0.6),
    # Copyright (source-book heuristic)
    ("all rights reserved", ModerationCategory.COPYRIGHT, 0.5),
    # Profanity
    ("damn", ModerationCategory.PROFANITY, 0.3),
)

#: Regexes that mine PII out of text deterministically (no model needed).
_SSN_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_EMAIL_RE = re.compile(r"\b[\w.%+-]+@[\w.-]+\.[A-Za-z]{2,}\b")


class KeywordClassifier:
    """A deterministic keyword/regex classifier — the offline + test default.

    Faithful enough to drive **every** taxonomy category, so tests assert real
    routing. It is also a perfectly serviceable cheap pre-filter in production: a
    book whose source text trips ``csam`` is rejected before a single model token
    is spent. The vision path can only act on bytes deterministically, so it keys
    off injected ``frame_labels`` (a test handle) and otherwise returns SAFE — a
    real deployment swaps in :class:`ModelVisionClassifier`.
    """

    name = "keyword"

    def __init__(self, *, frame_labels: dict[bytes, list[ContentLabel]] | None = None) -> None:
        # Optional explicit per-frame labels so vision-path tests are deterministic.
        self._frame_labels = frame_labels or {}

    async def classify_text(self, text: str, *, surface: Surface) -> ClassificationResult:
        labels = self._scan_text(text)
        return ClassificationResult(
            surface=surface,
            labels=labels or [ContentLabel.of(ModerationCategory.SAFE, 0.0)],
            classifier=self.name,
        )

    async def classify_frames(
        self, frames: list[bytes], *, surface: Surface
    ) -> ClassificationResult:
        labels: list[ContentLabel] = []
        for fr in frames:
            labels.extend(self._frame_labels.get(fr, []))
        return ClassificationResult(
            surface=surface,
            labels=labels or [ContentLabel.of(ModerationCategory.SAFE, 0.0)],
            classifier=self.name,
        )

    @staticmethod
    def _scan_text(text: str) -> list[ContentLabel]:
        lowered = text.lower()
        labels: list[ContentLabel] = []
        for term, category, score in _KEYWORD_RULES:
            if term in lowered:
                labels.append(ContentLabel.of(category, score, rationale=f"matched {term!r}"))
        if _SSN_RE.search(text):
            labels.append(ContentLabel.of(ModerationCategory.PII, 0.7, rationale="SSN pattern"))
        if _EMAIL_RE.search(text):
            labels.append(
                ContentLabel.of(ModerationCategory.PII, 0.35, rationale="email pattern")
            )
        return labels


# --------------------------------------------------------------------------- #
# Model-backed implementations (production) — NEVER invoked by the unit suite.
# --------------------------------------------------------------------------- #

#: Strict JSON contract the model is asked to emit, one entry per category fired.
_MODEL_INSTRUCTION = (
    "You are a content-safety classifier. Read the input and return ONLY JSON: "
    '{"labels": [{"category": <one of the taxonomy categories>, "score": <0..1>, '
    '"rationale": <short string>}]}. Use the taxonomy categories exactly. If the '
    'content is clean, return {"labels": [{"category": "safe", "score": 0.0}]}. '
    "Score is your confidence that the category applies AND how severe it is."
)

_VALID_CATEGORIES = {c.value for c in ModerationCategory}


def _labels_from_model(raw: object) -> list[ContentLabel]:
    """Project a model's ``{"labels": [...]}`` reply into typed labels (defensive)."""
    labels: list[ContentLabel] = []
    items = raw.get("labels", []) if isinstance(raw, dict) else []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        cat = str(item.get("category", "")).lower()
        if cat not in _VALID_CATEGORIES:
            cat = ModerationCategory.OTHER.value
        try:
            score = float(item.get("score", 0.0))
        except (TypeError, ValueError):
            score = 0.0
        rationale = item.get("rationale")
        labels.append(
            ContentLabel.of(
                ModerationCategory(cat),
                max(0.0, min(1.0, score)),
                rationale=str(rationale) if rationale is not None else None,
            )
        )
    return labels


class ModelTextClassifier:
    """Text classifier backed by the shared chat provider (production lane).

    Pairs a cheap keyword pre-filter with a model call so an obvious hit short-
    circuits without spend; the pre-filter's hits are merged with the model's so
    the model can never *hide* a zero-tolerance term. A model/transport failure
    returns a ``degraded`` SAFE-plus-prefilter result (never raises).
    """

    name = "model_text"

    def __init__(self, providers: Providers, *, settings: Settings) -> None:
        self._providers = providers
        self._settings = settings
        self._prefilter = KeywordClassifier()

    async def classify_text(self, text: str, *, surface: Surface) -> ClassificationResult:
        prefilter = KeywordClassifier._scan_text(text)
        try:
            raw = await self._providers.chat.chat_json(
                [
                    {"role": "system", "content": _MODEL_INSTRUCTION},
                    {"role": "user", "content": text[:8000]},
                ],
                self._settings.chat_model_adapter,
                temperature=0.0,
            )
            model_labels = _labels_from_model(raw)
        except Exception as exc:  # noqa: BLE001 - never let a provider blip crash a gate
            logger.warning("moderation.text_classifier.degraded", error=str(exc))
            return ClassificationResult(
                surface=surface,
                labels=prefilter or [ContentLabel.of(ModerationCategory.SAFE, 0.0)],
                classifier=self.name,
                degraded=True,
            )
        merged = _merge_labels(prefilter, model_labels)
        return ClassificationResult(
            surface=surface,
            labels=merged or [ContentLabel.of(ModerationCategory.SAFE, 0.0)],
            classifier=self.name,
        )


class ModelVisionClassifier:
    """Frame classifier backed by the shared VL provider (production lane).

    Samples the supplied frames and asks the VL model for taxonomy labels. A
    failure returns a ``degraded`` SAFE result (never raises). Frame sampling is
    bounded so a long clip's frame list never balloons the request.
    """

    name = "model_vision"
    max_frames = 4

    def __init__(self, providers: Providers, *, settings: Settings) -> None:
        self._providers = providers
        self._settings = settings

    async def classify_frames(
        self, frames: list[bytes], *, surface: Surface
    ) -> ClassificationResult:
        if not frames:
            return ClassificationResult(
                surface=surface,
                labels=[ContentLabel.of(ModerationCategory.SAFE, 0.0)],
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
            labels = _labels_from_model(raw)
        except Exception as exc:  # noqa: BLE001 - never let a provider blip crash a gate
            logger.warning("moderation.vision_classifier.degraded", error=str(exc))
            return ClassificationResult(
                surface=surface,
                labels=[ContentLabel.of(ModerationCategory.SAFE, 0.0)],
                classifier=self.name,
                degraded=True,
            )
        return ClassificationResult(
            surface=surface,
            labels=labels or [ContentLabel.of(ModerationCategory.SAFE, 0.0)],
            classifier=self.name,
        )


class CompositeClassifier:
    """Glue a text classifier and a vision classifier into one :class:`ContentClassifier`."""

    def __init__(self, *, text: TextClassifier, vision: VisionClassifier, name: str) -> None:
        self._text = text
        self._vision = vision
        self.name = name

    async def classify_text(self, text: str, *, surface: Surface) -> ClassificationResult:
        return await self._text.classify_text(text, surface=surface)

    async def classify_frames(
        self, frames: list[bytes], *, surface: Surface
    ) -> ClassificationResult:
        return await self._vision.classify_frames(frames, surface=surface)


def build_default_classifier(
    providers: Providers | None = None, *, settings: Settings | None = None
) -> ContentClassifier:
    """Build the production classifier, or the offline keyword fake when no providers.

    The composition root passes the wired providers; without them (or in tests)
    the keyword classifier is returned, so importing this module never forces a
    provider/network dependency.
    """
    if providers is None or settings is None:
        return KeywordClassifier()
    return CompositeClassifier(
        text=ModelTextClassifier(providers, settings=settings),
        vision=ModelVisionClassifier(providers, settings=settings),
        name="model_composite",
    )


def _merge_labels(a: list[ContentLabel], b: list[ContentLabel]) -> list[ContentLabel]:
    """Merge two label lists, keeping the highest score per category."""
    best: dict[ModerationCategory, ContentLabel] = {}
    for lab in [*a, *b]:
        cur = best.get(lab.category)
        if cur is None or lab.score > cur.score:
            best[lab.category] = lab
    return list(best.values())


def _sample(frames: list[bytes], k: int) -> list[bytes]:
    """Evenly sample at most ``k`` frames (first + spread) for a bounded request."""
    if len(frames) <= k:
        return list(frames)
    step = len(frames) / k
    return [frames[int(i * step)] for i in range(k)]


__all__ = [
    "CompositeClassifier",
    "ContentClassifier",
    "KeywordClassifier",
    "ModelTextClassifier",
    "ModelVisionClassifier",
    "TextClassifier",
    "VisionClassifier",
    "build_default_classifier",
]
