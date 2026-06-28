"""Narration prosody planning — emphasis + breaks from punctuation (§9.4).

Qwen3-TTS does not expose per-word prosody knobs on the plain narration path (only
the *instruct* model takes a style instruction), so :mod:`app.providers.tts` records
``speed``/``pitch`` without forcing them onto the request. This module adds the layer
that *can* be honoured without a paid instruct call: a **pure, deterministic prosody
plan** derived from the narration text itself — which words to stress, where to
breathe, and how the pace should rise/fall — that

* feeds the instruct model a single concise style instruction when it is available
  (a future opt-in), and
* lets the §9.4 sync-map highlight pulse on stressed words / pause on breaks even on
  the plain path (the plan rides alongside the word timings, the client may use it).

Everything here is pure (text in, plan out) and exhaustively unit-testable. It never
calls a model and never spends. Stress is heuristic — capitalised emphasis,
exclamation, italic/underscore markup, and long content words — never a pronunciation
model; breaks come from sentence and clause punctuation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

#: A token = a run of word characters with optional surrounding markup/punctuation.
_TOKEN_RE = re.compile(r"\S+")
#: Strip surrounding punctuation/markup to get the bare word for stress scoring.
_BARE_RE = re.compile(r"^[\W_]+|[\W_]+$")
#: Common function words that almost never carry stress (kept short + safe).
_FUNCTION_WORDS = frozenset(
    {
        "a", "an", "the", "and", "or", "but", "of", "to", "in", "on", "at", "by",
        "for", "with", "as", "is", "was", "were", "be", "been", "am", "are", "it",
        "its", "he", "she", "they", "we", "you", "i", "his", "her", "their", "our",
        "that", "this", "these", "those", "so", "if", "then", "than", "from", "into",
    }
)


class BreakStrength(StrEnum):
    """How long a pause follows a token (drives breath / highlight dwell)."""

    NONE = "none"
    WEAK = "weak"  # comma / semicolon / clause break
    STRONG = "strong"  # sentence end (. ! ?)


#: Approximate pause seconds per break strength (the dwell the client may honour).
_BREAK_SECONDS: dict[BreakStrength, float] = {
    BreakStrength.NONE: 0.0,
    BreakStrength.WEAK: 0.18,
    BreakStrength.STRONG: 0.42,
}


@dataclass(frozen=True, slots=True)
class ProsodyMark:
    """The prosody decision for one narrated token (positional, 1:1 with words).

    Attributes:
        text: the token as it appears (with its original markup/punctuation).
        stress: 0..1 — how emphasised the word is (drives a highlight pulse / a
            stress hint in the instruct style string).
        break_after: the pause strength following this token.
        break_s: the pause length in seconds for ``break_after``.
    """

    text: str
    stress: float
    break_after: BreakStrength
    break_s: float


@dataclass(frozen=True, slots=True)
class ProsodyPlan:
    """A whole utterance's prosody: per-token marks + a summary style instruction."""

    marks: tuple[ProsodyMark, ...]
    #: A concise natural-language style instruction for the instruct TTS model, e.g.
    #: "Read warmly, emphasising 'storm' and 'never'; pause at sentence ends." Built
    #: deterministically from the marks; safe to pass to ``qwen3-tts-instruct-*``.
    style_instruction: str

    @property
    def stressed_words(self) -> list[str]:
        """Bare stressed words in order (the emphasis the highlight pulses on)."""
        return [bare_word(m.text) for m in self.marks if m.stress >= _STRESS_THRESHOLD]


#: A token at or above this stress is "emphasised" for the instruction summary.
_STRESS_THRESHOLD = 0.6
#: Content words this long are mildly stressed even without other cues.
_LONG_WORD_LEN = 7


def bare_word(token: str) -> str:
    """The token with surrounding punctuation/markup stripped, lowercased."""
    return _BARE_RE.sub("", token).lower()


def _break_after(token: str) -> BreakStrength:
    trailing = token.rstrip("\"'”’)")
    if trailing.endswith((".", "!", "?", "…")):
        return BreakStrength.STRONG
    if trailing.endswith((",", ";", ":", "—", "–")):
        return BreakStrength.WEAK
    return BreakStrength.NONE


def _stress(token: str) -> float:
    """Heuristic 0..1 stress for a token (pure)."""
    bare = bare_word(token)
    if not bare or bare in _FUNCTION_WORDS:
        return 0.1
    score = 0.45  # baseline for a content word
    # Markup emphasis: *word*, _word_, or surrounding underscores/asterisks.
    if re.search(r"[*_]", token):
        score += 0.3
    # ALL-CAPS (≥2 letters) is a strong shout.
    letters = [c for c in token if c.isalpha()]
    if len(letters) >= 2 and all(c.isupper() for c in letters):
        score += 0.35
    # Exclamation right after the word lifts it.
    if token.rstrip("\"'”’)").endswith("!"):
        score += 0.2
    # Long content words carry a little more weight.
    if len(bare) >= _LONG_WORD_LEN:
        score += 0.15
    return round(min(1.0, score), 3)


def plan_prosody(text: str) -> ProsodyPlan:
    """Build a deterministic :class:`ProsodyPlan` for narration ``text`` (pure).

    Tokenises on whitespace (1:1 with the words the TTS/aligner will see), scores
    each token's stress, classifies the pause after it, and summarises the whole into
    one instruct-model style string. Empty/whitespace text yields an empty plan with
    a neutral instruction.
    """
    tokens = _TOKEN_RE.findall(text or "")
    if not tokens:
        return ProsodyPlan(marks=(), style_instruction="Read in a calm, natural voice.")
    marks: list[ProsodyMark] = []
    for token in tokens:
        strength = _break_after(token)
        marks.append(
            ProsodyMark(
                text=token,
                stress=_stress(token),
                break_after=strength,
                break_s=_BREAK_SECONDS[strength],
            )
        )
    return ProsodyPlan(marks=tuple(marks), style_instruction=_summarise(marks))


def _summarise(marks: list[ProsodyMark]) -> str:
    """A concise instruct-model style instruction from the per-token marks."""
    emphasised = [bare_word(m.text) for m in marks if m.stress >= _STRESS_THRESHOLD]
    has_strong = any(m.break_after is BreakStrength.STRONG for m in marks)
    exclaim = any(m.text.rstrip("\"'”’)").endswith("!") for m in marks)
    tone = "with bright energy" if exclaim else "in a calm, natural voice"
    parts = [f"Read {tone}"]
    if emphasised:
        # Keep the instruction short — cap the emphasised list.
        shown = emphasised[:5]
        quoted = ", ".join(f"'{w}'" for w in shown)
        parts.append(f"emphasising {quoted}")
    if has_strong:
        parts.append("pausing at sentence ends")
    return "; ".join(parts) + "."


def apply_to_words(words: list[object], plan: ProsodyPlan) -> list[float]:
    """Per-word stress aligned to a list of timed words (positional, pure).

    Returns a stress value for each word in ``words`` (0..1), taken positionally from
    the plan; words past the plan's length get a neutral 0.4. Lets the sync map / a
    viseme track read the emphasis without re-parsing the text.
    """
    out: list[float] = []
    for i, _word in enumerate(words):
        out.append(plan.marks[i].stress if i < len(plan.marks) else 0.4)
    return out


__all__ = [
    "BreakStrength",
    "ProsodyMark",
    "ProsodyPlan",
    "apply_to_words",
    "bare_word",
    "plan_prosody",
]
