"""Discourse-mode + interiority detection (free indirect discourse, §10).

Classifies *how* a beat's content reaches the reader:

* **dialogue** — quoted speech dominates;
* **interior_monologue** — unquoted first/second-person present-tense thought,
  or italic-style internal reportage;
* **free_indirect** — the literary middle ground: third-person past-tense
  narration carrying a character's *diction and feeling* (questions,
  exclamations, evaluative/colloquial words) without a "she thought" tag — the
  reader hears the character's mind through the narrator's grammar;
* **narration** — plain external reportage (the default).

Why it matters for the film: an interiority/free-indirect beat is NOT a literal
action to stage — it is a subjective image (the character's mental picture,
mood-coloured light, a dream logic). The Cinematographer reads ``discourse`` to
decide between a literal exterior shot and a subjective/POV shot.

Pure and deterministic; an LLM pass may refine the call. The signal is built
from sentence shape + lexical cues + the dialogue density computed in
:mod:`.dialogue`.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.agents.contracts import DiscourseMode

from .dialogue import dialogue_density
from .text_utils import split_sentences, strip_quotes, words

#: Verbs that explicitly tag interior thought ("she thought", "he wondered").
_THOUGHT_TAGS = frozenset({
    "thought", "wondered", "mused", "reflected", "realized", "realised",
    "remembered", "recalled", "imagined", "supposed", "figured", "reckoned",
})

#: Evaluative / colloquial markers characteristic of free indirect discourse —
#: the narrator borrowing the character's idiom.
_FID_COLOURING = frozenset({
    "surely", "of course", "obviously", "after all", "really", "honestly",
    "ridiculous", "absurd", "wonderful", "awful", "terrible", "splendid",
    "damn", "blast", "good", "lord", "heavens", "god", "well", "indeed",
    "perhaps", "maybe", "no", "yes", "never", "always", "how", "why", "what",
})

_PRESENT_FIRST = frozenset({"i", "my", "me", "we", "our"})


@dataclass(frozen=True)
class DiscourseAnalysis:
    """A beat's discourse mode plus the interior content when it has any."""

    mode: DiscourseMode
    interiority: str | None
    #: 0..1 — how strongly the free-indirect signal fired (for LLM override).
    fid_strength: float


def classify_discourse(text: str) -> DiscourseAnalysis:
    """Classify a beat's discourse mode and extract any interior content.

    Order of precedence: dialogue-dominant ⇒ DIALOGUE; explicit thought tag or
    first-person present-tense interior ⇒ INTERIOR_MONOLOGUE; strong
    free-indirect colouring in third-person narration ⇒ FREE_INDIRECT;
    otherwise NARRATION.
    """
    if not text.strip():
        return DiscourseAnalysis(DiscourseMode.NARRATION, None, 0.0)

    if dialogue_density(text) >= 0.5:
        return DiscourseAnalysis(DiscourseMode.DIALOGUE, None, 0.0)

    narration = strip_quotes(text)
    low_tokens = words(narration)
    low_set = set(low_tokens)

    # Explicit interior monologue: a thought tag, or unquoted 1st-person present.
    if low_set & _THOUGHT_TAGS or _is_first_person_present(narration):
        return DiscourseAnalysis(
            DiscourseMode.INTERIOR_MONOLOGUE, narration.strip() or None, 1.0
        )

    fid = _free_indirect_strength(narration)
    if fid >= 0.5:
        return DiscourseAnalysis(DiscourseMode.FREE_INDIRECT, narration.strip() or None, fid)

    return DiscourseAnalysis(DiscourseMode.NARRATION, None, fid)


def _is_first_person_present(narration: str) -> bool:
    toks = words(narration)
    if not toks:
        return False
    first = sum(1 for t in toks if t in _PRESENT_FIRST)
    return first / len(toks) >= 0.08


def _free_indirect_strength(narration: str) -> float:
    """Score 0..1 that third-person narration carries a character's mind.

    Signals: rhetorical questions / exclamations (the character's affect leaking
    through), evaluative/colloquial colouring words, and sentence fragments. The
    presence of any third-person pronoun keeps it from firing on pure dialogue.
    """
    sents = split_sentences(narration)
    if not sents:
        return 0.0
    toks = words(narration)
    if not toks:
        return 0.0
    has_third = any(t in {"he", "she", "they", "his", "her", "their"} for t in toks)
    if not has_third:
        return 0.0

    questions = sum(1 for s in sents if s.text.rstrip().endswith("?"))
    exclaims = sum(1 for s in sents if s.text.rstrip().endswith("!"))
    colour = sum(1 for t in toks if t in _FID_COLOURING)

    affect = (questions + exclaims) / len(sents)
    colouring = min(1.0, colour / max(1, len(toks) / 12))
    score = 0.6 * affect + 0.4 * colouring
    return round(min(1.0, score), 3)


def is_subjective(mode: DiscourseMode) -> bool:
    """Whether a beat should be staged as a subjective/POV image, not literally."""
    return mode in (DiscourseMode.INTERIOR_MONOLOGUE, DiscourseMode.FREE_INDIRECT)


__all__ = ["DiscourseAnalysis", "classify_discourse", "is_subjective"]
