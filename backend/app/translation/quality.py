"""Quality estimation + back-translation round-trip checks.

Translation quality estimation (QE) scores a translation *without a reference*.
We can't run a learned QE model in the backend (heavy dependency, and tests must
be deterministic), so the default estimator combines cheap, meaningful signals
that catch the failures that actually hurt this product:

* **Markup integrity** — every protected placeholder/tag reappears exactly once
  (a hard requirement; a violation caps the score low). Independent of meaning.
* **Glossary/DNT compliance** — every forced/locked term is present.
* **Length plausibility** — a translation that is a tiny fraction or a huge
  multiple of the source length is suspicious (truncation / runaway). The
  expected ratio is language-pair-aware (CJK is terser than German).
* **Non-empty / non-identical** — an empty result, or output identical to the
  source when the languages differ, signals the model gave up.
* **Back-translation agreement (optional)** — translate the output back to the
  source language and measure similarity to the original. High agreement is a
  strong correctness signal; this is the §9.5 "self-correcting" idea applied to
  text, and it is the one check that costs a second provider call (so it is
  opt-in per request).

The combined score is in ``[0, 1]``; below the review threshold the segment is
flagged for human post-edit (:mod:`.review`).
"""

from __future__ import annotations

from dataclasses import dataclass

from .glossary import Glossary
from .languages import same_language
from .markup import verify_roundtrip
from .memory_store import similarity_ratio

# Heuristic expected target/source character-length ratios by primary subtag.
# Used only for the length-plausibility band; defaults to 1.0 when unknown.
_LENGTH_RATIO: dict[str, float] = {
    "zh-Hans": 0.55,
    "zh-Hant": 0.55,
    "ja": 0.7,
    "ko": 0.75,
    "de": 1.18,
    "fi": 1.2,
    "ar": 0.95,
    "he": 0.85,
    "es": 1.1,
    "fr": 1.12,
}


@dataclass(frozen=True, slots=True)
class QualityReport:
    """The outcome of estimating one segment's quality."""

    score: float
    warnings: tuple[str, ...]
    markup_ok: bool
    glossary_ok: bool
    length_ok: bool
    back_translation_ratio: float | None = None

    @property
    def passed(self) -> bool:
        return self.score >= 0.6 and self.markup_ok and self.glossary_ok


def _expected_ratio(target_lang: str) -> float:
    from .languages import get_language

    try:
        lang = get_language(target_lang)
    except Exception:  # noqa: BLE001 - unknown lang → neutral expectation
        return 1.0
    return _LENGTH_RATIO.get(lang.tag, _LENGTH_RATIO.get(lang.primary_subtag, 1.0))


def length_plausibility(source: str, translated: str, target_lang: str) -> tuple[bool, float]:
    """Score how plausible the translation's length is. Returns (ok, penalty 0..1)."""
    src_len = max(len(source.strip()), 1)
    tgt_len = len(translated.strip())
    if tgt_len == 0:
        return (False, 1.0)
    expected = src_len * _expected_ratio(target_lang)
    ratio = tgt_len / expected if expected else 1.0
    # Acceptable band: 0.4x .. 2.5x of the expected length.
    if 0.4 <= ratio <= 2.5:
        return (True, 0.0)
    # Penalty grows with distance outside the band, capped at 1.
    if ratio < 0.4:
        return (False, min((0.4 - ratio) / 0.4, 1.0))
    return (False, min((ratio - 2.5) / 5.0, 1.0))


def estimate_quality(
    *,
    source: str,
    translated: str,
    source_lang: str,
    target_lang: str,
    glossary: Glossary | None = None,
    back_translation: str | None = None,
) -> QualityReport:
    """Combine the cheap signals into a single ``[0, 1]`` quality score.

    ``back_translation`` (target→source) is optional; when provided its
    similarity to the source adds a strong correctness term.
    """
    warnings: list[str] = []
    score = 1.0

    # 1) Markup integrity (hard).
    markup_warnings = verify_roundtrip(source, translated)
    markup_ok = not markup_warnings
    if not markup_ok:
        warnings.extend(markup_warnings)
        score -= 0.5  # a markup break is severe

    # 2) Glossary / DNT compliance (hard-ish).
    glossary_ok = True
    if glossary is not None:
        gloss_warnings = glossary.verify(source, translated, target_lang=target_lang)
        if gloss_warnings:
            glossary_ok = False
            warnings.extend(gloss_warnings)
            score -= 0.25

    # 3) Empty / identical when languages differ.
    if not translated.strip():
        warnings.append("empty translation")
        score -= 0.6
    elif not same_language(source_lang, target_lang) and translated.strip() == source.strip():
        warnings.append("output identical to source (untranslated?)")
        score -= 0.3

    # 4) Length plausibility (soft).
    length_ok, length_penalty = length_plausibility(source, translated, target_lang)
    if not length_ok:
        warnings.append(f"implausible length (penalty {length_penalty:.2f})")
        score -= 0.2 * length_penalty

    # 5) Back-translation agreement (strong, optional).
    bt_ratio: float | None = None
    if back_translation is not None:
        bt_ratio = similarity_ratio(source.strip().lower(), back_translation.strip().lower())
        # Map agreement into a ±0.2 adjustment around a 0.55 neutral point.
        score += (bt_ratio - 0.55) * 0.4
        if bt_ratio < 0.4:
            warnings.append(f"low back-translation agreement ({bt_ratio:.2f})")

    score = max(0.0, min(1.0, score))
    return QualityReport(
        score=round(score, 4),
        warnings=tuple(warnings),
        markup_ok=markup_ok,
        glossary_ok=glossary_ok,
        length_ok=length_ok,
        back_translation_ratio=bt_ratio,
    )


__all__ = [
    "QualityReport",
    "estimate_quality",
    "length_plausibility",
]
