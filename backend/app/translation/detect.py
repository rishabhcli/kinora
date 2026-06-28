"""Language detection: a fast script/stopword heuristic + a pluggable seam.

When a translation request arrives without a declared source language we have to
guess it. A full statistical model (fastText/CLD3) is overkill for the backend
and pulls a heavy dependency, so the default detector is a dependency-free
heuristic that is *good enough* to (a) route a segment to the right glossary and
(b) short-circuit a passthrough when source == target:

1. **Script detection** — count code points by Unicode block. A run of CJK,
   Cyrillic, Arabic, Hebrew, Devanagari, etc. is decisive on its own (those
   scripts map to a small set of candidate languages).
2. **Stopword scoring** — for Latin-script text, score against per-language
   stopword sets; the highest-scoring language wins above a confidence floor.

Detection is *injectable*: :class:`Detector` is the protocol, and a caller can
swap in a model-backed detector without touching the pipeline. The heuristic is
deterministic, so tests need no model and spend nothing.
"""

from __future__ import annotations

import unicodedata
from collections import Counter
from dataclasses import dataclass
from typing import Protocol

from .languages import Language, get_language


def _words(joined: str) -> frozenset[str]:
    """Build a stopword set from a space-separated literal (keeps lines short)."""
    return frozenset(joined.split())


# Per-language stopword sets for Latin-script disambiguation. Small, high-signal
# function words — enough to separate the major Romance/Germanic/Slavic-Latin
# languages without a model. Lowercased; matched as whole tokens.
_STOPWORDS: dict[str, frozenset[str]] = {
    "en": _words("the a an and or but of to in is are was were that this with for it"),
    "es": _words("el la los las un una y o pero de que en es son con para por como no"),
    "fr": _words("le la les un une et ou mais de que dans est sont avec pour par comme ne"),
    "de": _words("der die das ein eine und oder aber von dass in ist sind mit für durch nicht"),
    "it": _words("il lo la i gli le un una e o ma di che in è sono con per come non"),
    "pt": _words("o a os as um uma e ou mas de que em é são com para por como não"),
    "nl": _words("de het een en of maar van dat in is zijn met voor door niet ook"),
    "pl": _words("i lub ale w na z do że jest są nie się to o od po za który"),
    "tr": _words("ve veya ama bir bu da de ki için ile gibi çok daha en var yok"),
    "id": _words("dan atau tetapi yang di ke dari ini itu dengan untuk adalah tidak akan"),
    "vi": _words("và hoặc nhưng của là một các này đó với cho không được trong"),
}

# Script block → candidate canonical tags (first is the default guess).
_SCRIPT_CANDIDATES: dict[str, tuple[str, ...]] = {
    "CJK": ("zh-Hans", "ja"),
    "HIRAGANA": ("ja",),
    "KATAKANA": ("ja",),
    "HANGUL": ("ko",),
    "CYRILLIC": ("ru", "uk"),
    "ARABIC": ("ar", "fa", "ur"),
    "HEBREW": ("he",),
    "DEVANAGARI": ("hi",),
    "THAI": ("th",),
}


@dataclass(frozen=True, slots=True)
class Detection:
    """A detection outcome.

    Attributes:
        language: The resolved :class:`Language`.
        confidence: Heuristic confidence in ``[0, 1]``.
        method: ``"script"`` or ``"stopword"`` or ``"default"`` — for telemetry.
    """

    language: Language
    confidence: float
    method: str


class Detector(Protocol):
    """The injectable language-detection seam."""

    def detect(self, text: str, *, default: str = "en") -> Detection:
        """Return the most likely language of ``text``."""
        ...


def _script_of(ch: str) -> str | None:
    """Coarse Unicode-block bucket for a character (None for ASCII/punct)."""
    code = ord(ch)
    if 0x3040 <= code <= 0x309F:
        return "HIRAGANA"
    if 0x30A0 <= code <= 0x30FF:
        return "KATAKANA"
    if 0xAC00 <= code <= 0xD7A3 or 0x1100 <= code <= 0x11FF:
        return "HANGUL"
    if 0x4E00 <= code <= 0x9FFF or 0x3400 <= code <= 0x4DBF:
        return "CJK"
    if 0x0400 <= code <= 0x04FF:
        return "CYRILLIC"
    if 0x0600 <= code <= 0x06FF or 0x0750 <= code <= 0x077F or 0xFB50 <= code <= 0xFDFF:
        return "ARABIC"
    if 0x0590 <= code <= 0x05FF:
        return "HEBREW"
    if 0x0900 <= code <= 0x097F:
        return "DEVANAGARI"
    if 0x0E00 <= code <= 0x0E7F:
        return "THAI"
    return None


class HeuristicDetector:
    """The default dependency-free detector (script + stopword scoring)."""

    def __init__(self, *, stopword_floor: float = 0.06) -> None:
        # Minimum stopword density to trust a Latin-script guess.
        self._floor = stopword_floor

    def detect(self, text: str, *, default: str = "en") -> Detection:
        stripped = text.strip()
        if not stripped:
            return Detection(get_language(default), 0.0, "default")

        # 1) Script signal. Count non-Latin scripted code points.
        script_counts: Counter[str] = Counter()
        letters = 0
        for ch in stripped:
            if not ch.isalpha():
                continue
            letters += 1
            bucket = _script_of(ch)
            if bucket is not None:
                script_counts[bucket] += 1
        if script_counts:
            dominant, count = script_counts.most_common(1)[0]
            if letters and count / letters >= 0.30:
                candidates = _SCRIPT_CANDIDATES.get(dominant)
                if candidates:
                    # Japanese kana is decisive even mixed with CJK han.
                    if dominant == "CJK" and (
                        script_counts["HIRAGANA"] or script_counts["KATAKANA"]
                    ):
                        return Detection(get_language("ja"), 0.95, "script")
                    conf = min(0.6 + count / max(letters, 1) * 0.4, 0.99)
                    return Detection(get_language(candidates[0]), conf, "script")

        # 2) Stopword scoring for Latin-script text.
        tokens = [t for t in _tokenize(stripped) if t]
        if tokens:
            scores: dict[str, int] = {}
            for lang, words in _STOPWORDS.items():
                scores[lang] = sum(1 for t in tokens if t in words)
            best_lang = max(scores, key=lambda k: scores[k])
            density = scores[best_lang] / len(tokens)
            if density >= self._floor:
                conf = min(0.5 + density * 2.0, 0.97)
                return Detection(get_language(best_lang), conf, "stopword")

        return Detection(get_language(default), 0.2, "default")


def _tokenize(text: str) -> list[str]:
    """Lowercase whitespace/punct tokenizer adequate for stopword scoring."""
    out: list[str] = []
    cur: list[str] = []
    for ch in text.lower():
        category = unicodedata.category(ch)
        if category.startswith("L") or category == "Mn":
            cur.append(ch)
        else:
            if cur:
                out.append("".join(cur))
                cur = []
    if cur:
        out.append("".join(cur))
    return out


#: A module-level default instance (stateless, safe to share).
default_detector: Detector = HeuristicDetector()


def detect_language(
    text: str, *, default: str = "en", detector: Detector | None = None
) -> Detection:
    """Detect the language of ``text`` (uses the heuristic detector by default)."""
    return (detector or default_detector).detect(text, default=default)


__all__ = [
    "Detection",
    "Detector",
    "HeuristicDetector",
    "default_detector",
    "detect_language",
]
