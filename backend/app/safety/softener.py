"""Intent-preserving prompt auto-softening.

The product insight: a faithful book adaptation will routinely describe violence,
sexuality, gore, and intense imagery that a video provider's policy refuses *as
literally phrased*, yet the *scene* is admissible if framed tastefully — implied
off-frame, cut on the moment, rendered in shadow. Hard-blocking such a prompt
would drop a legitimate beat of the film. So the gateway first tries to **rewrite
the prompt to satisfy the policy while preserving the dramatic intent**, recording
exactly what it changed (an explainable, auditable diff), and only escalates to
QUARANTINE/BLOCK when softening cannot resolve the offending content.

Two layers, mirroring the classifier seam:

* :class:`RuleSoftener` — a **deterministic, no-network** rewriter: a curated map
  of explicit phrasings → tasteful, cinematic substitutions, plus per-category
  "tasteful framing" clauses appended when a softenable category is still hot.
  This is the test/offline default and a perfectly serviceable production
  pre-pass (it costs nothing and resolves the common cases).
* :class:`ModelSoftener` — a production lane that asks the chat provider to
  rewrite the prompt under a strict instruction; falls back to the rule softener
  on any failure so a provider blip never blocks a softenable prompt.

Invariants the tests pin:

* The softener **never empties** a prompt — an all-redaction is a *block*, not a
  transform. :attr:`SofteningResult.intent_preserved` stays True.
* The softener **never touches** a non-softenable category (hate, CSAM,
  extremism, self-harm): those are reported in ``unsoftenable`` for the gateway
  to escalate, not silently dropped.
* Softening is **idempotent enough**: re-running on an already-soft prompt yields
  ``changed=False``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

from app.core.logging import get_logger
from app.safety.classifier import KeywordSafetyClassifier
from app.safety.contracts import SafetyCategory, SofteningResult
from app.safety.rules import RuleDecision, softenable_categories, unsoftenable_blocking_categories
from app.safety.taxonomy import is_softenable

if TYPE_CHECKING:
    from app.core.config import Settings
    from app.providers import Providers

logger = get_logger("app.safety.softener")


@runtime_checkable
class PromptSoftener(Protocol):
    """Rewrites a prompt to satisfy policy while preserving intent."""

    async def soften(self, prompt: str, *, decision: RuleDecision) -> SofteningResult:
        """Return the softened prompt + the diff of what changed."""
        ...


#: Explicit phrasing → tasteful cinematic substitution. Ordered longest-first so a
#: specific phrase wins over a substring. Case-insensitive whole-phrase replace.
_SUBSTITUTIONS: tuple[tuple[str, str], ...] = (
    # Gore — the worst, most literal phrasings get the strongest reframing.
    ("graphic gore", "a grim aftermath implied in shadow"),
    ("disembowel", "mortally wound"),
    ("dismember", "gravely wound"),
    ("blood everywhere", "the dim, grim aftermath of a struggle"),
    ("pool of blood", "a dark stain on the floor"),
    ("bloody", "grim"),
    # Violence — keep the tension, lose the explicit act.
    ("graphic stabbing", "a sudden, tense struggle, the blow implied off-frame"),
    ("brutal beating", "a harsh, one-sided confrontation"),
    ("massacre", "a devastating, off-screen tragedy"),
    ("stab", "lunge at"),
    # Sexual — fade-to-black framing.
    ("graphic sex scene", "an intimate moment, tasteful and implied, cut before explicit"),
    ("explicit sex", "an implied intimate encounter, fading discreetly"),
    ("pornographic", "suggestive but non-explicit"),
    # Nudity — classical / implied framing.
    ("fully nude", "figure framed discreetly, shoulders-up or in silhouette"),
    ("naked", "bare-shouldered, framed discreetly"),
    ("nude", "discreetly framed, classical composition"),
)

#: Per-category "tasteful framing" clause appended when a softenable category is
#: still flagged after substitutions (belt-and-braces: nudges the provider toward
#: a compliant rendering even if the offending word survived).
_FRAMING_CLAUSES: dict[SafetyCategory, str] = {
    SafetyCategory.VIOLENCE: (
        "Depict the violence cinematically and non-graphically: tension and "
        "aftermath over explicit action, the decisive moment implied off-frame."
    ),
    SafetyCategory.GORE: (
        "Avoid explicit gore; convey the gravity through shadow, reaction shots, "
        "and atmosphere rather than graphic detail."
    ),
    SafetyCategory.SEXUAL: (
        "Keep any intimacy tasteful and non-explicit, framed suggestively and "
        "cutting away before anything explicit."
    ),
    SafetyCategory.NUDITY: (
        "Frame the figure discreetly (silhouette, shoulders-up, or classical "
        "composition); no explicit nudity."
    ),
    SafetyCategory.PROFANITY: (
        "Render dialogue and on-screen text without explicit profanity."
    ),
}


class RuleSoftener:
    """Deterministic, no-network intent-preserving softener (test/offline default)."""

    name = "rule"

    def __init__(self, *, max_clauses: int = 3) -> None:
        # Cap appended clauses so a busy scene's prompt does not balloon.
        self._max_clauses = max_clauses

    async def soften(self, prompt: str, *, decision: RuleDecision) -> SofteningResult:
        return self.soften_sync(prompt, decision=decision)

    def soften_sync(self, prompt: str, *, decision: RuleDecision) -> SofteningResult:
        """Pure synchronous core (the async ``soften`` just awaits this)."""
        unsoftenable = unsoftenable_blocking_categories(decision)
        targets = [c for c in softenable_categories(decision) if is_softenable(c)]

        transforms: list[str] = []
        softened = prompt

        # 1) Phrase substitutions (only those whose category is a softenable target,
        #    so we never reframe content the policy did not actually flag).
        target_set = set(targets)
        for phrase, replacement in _SUBSTITUTIONS:
            cat = _phrase_category(phrase)
            if phrase in softened.lower() and cat in target_set:
                softened = _ci_replace(softened, phrase, replacement)
                transforms.append(f"{cat.value}: {phrase!r} -> {replacement!r}")

        # 2) Re-scan and append a tasteful-framing clause for any softenable
        #    category still hot after substitution.
        rescanned = {f.category for f in KeywordSafetyClassifier.scan_text(softened) if f.positive}
        appended = 0
        resolved: list[SafetyCategory] = []
        for cat in targets:
            if cat in rescanned and cat in _FRAMING_CLAUSES and appended < self._max_clauses:
                softened = f"{softened.rstrip()} {_FRAMING_CLAUSES[cat]}"
                transforms.append(f"{cat.value}: appended tasteful-framing directive")
                appended += 1
            if cat not in rescanned:
                resolved.append(cat)

        changed = softened != prompt
        # Invariant: never empty the prompt. If substitution somehow stripped it
        # to whitespace, fall back to the original (a transform must preserve text).
        if not softened.strip():
            softened = prompt
            changed = False
        return SofteningResult(
            changed=changed,
            original_prompt=prompt,
            softened_prompt=softened,
            transforms=transforms,
            unsoftenable=sorted(unsoftenable, key=lambda c: c.value),
            resolved=sorted(set(resolved), key=lambda c: c.value),
        )


_MODEL_INSTRUCTION = (
    "You rewrite a film-shot prompt so it complies with a video model's content "
    "policy WITHOUT changing the dramatic intent of the scene. Keep the setting, "
    "characters, mood, and beat; render violence/sexuality/gore tastefully and "
    "non-graphically (implied off-frame, cut on the moment, shadow/silhouette). "
    "Never add new content. Never blank the prompt. Return ONLY JSON: "
    '{"prompt": <the rewritten prompt>, "notes": <one short line on what changed>}.'
)


class ModelSoftener:
    """Production softener backed by the chat provider, with a rule fallback.

    Asks the model to rewrite the prompt under a strict instruction; on any failure
    (or an empty/over-long reply) it falls back to :class:`RuleSoftener` so a
    softenable prompt is never blocked just because the model lane hiccuped.
    """

    name = "model"

    def __init__(self, providers: Providers, *, settings: Settings) -> None:
        self._providers = providers
        self._settings = settings
        self._fallback = RuleSoftener()

    async def soften(self, prompt: str, *, decision: RuleDecision) -> SofteningResult:
        unsoftenable = unsoftenable_blocking_categories(decision)
        targets = [c for c in softenable_categories(decision) if is_softenable(c)]
        if not targets:
            return SofteningResult(
                changed=False,
                original_prompt=prompt,
                softened_prompt=prompt,
                unsoftenable=sorted(unsoftenable, key=lambda c: c.value),
            )
        try:
            raw = await self._providers.chat.chat_json(
                [
                    {"role": "system", "content": _MODEL_INSTRUCTION},
                    {"role": "user", "content": prompt[:8000]},
                ],
                self._settings.chat_model_adapter,
                temperature=0.2,
            )
            rewritten = str(raw.get("prompt", "")).strip() if isinstance(raw, dict) else ""
            note = str(raw.get("notes", "")).strip() if isinstance(raw, dict) else ""
        except Exception as exc:  # noqa: BLE001 - never block a softenable prompt on a blip
            logger.warning("safety.softener.degraded", error=str(exc))
            return await self._fallback.soften(prompt, decision=decision)
        if not rewritten:
            return await self._fallback.soften(prompt, decision=decision)
        changed = rewritten != prompt
        return SofteningResult(
            changed=changed,
            original_prompt=prompt,
            softened_prompt=rewritten,
            transforms=[note] if note else ["model rewrite"],
            unsoftenable=sorted(unsoftenable, key=lambda c: c.value),
        )


def _phrase_category(phrase: str) -> SafetyCategory:
    """The category a substitution phrase belongs to (drives target gating)."""
    findings = KeywordSafetyClassifier.scan_text(phrase)
    for f in findings:
        if f.positive:
            return f.category
    return SafetyCategory.OTHER


def _ci_replace(text: str, phrase: str, replacement: str) -> str:
    """Case-insensitive whole-phrase replacement preserving the rest of the text."""
    lowered = text.lower()
    target = phrase.lower()
    out: list[str] = []
    i = 0
    while True:
        idx = lowered.find(target, i)
        if idx == -1:
            out.append(text[i:])
            break
        out.append(text[i:idx])
        out.append(replacement)
        i = idx + len(target)
    return "".join(out)


def build_default_softener(
    providers: Providers | None = None, *, settings: Settings | None = None
) -> PromptSoftener:
    """The production softener, or the deterministic rule softener when no providers."""
    if providers is None or settings is None:
        return RuleSoftener()
    return ModelSoftener(providers, settings=settings)


__all__ = [
    "ModelSoftener",
    "PromptSoftener",
    "RuleSoftener",
    "build_default_softener",
]
