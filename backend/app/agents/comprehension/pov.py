"""Point-of-view + unreliable-narrator analysis (§4.2 multi-POV, §10).

A multi-POV novel narrates different beats from different vantages; a faithful
adaptation must render each beat from *its* POV (a first-person beat is the
camera's subjective view; a third-omniscient beat can roam). This pure module
classifies a beat's narrating person and, when third-limited, the focal
character — then flags telling that is likely **unreliable** (heavy hedging,
explicit deception, irony cues) so downstream agents treat its depicted facts as
*claims*, not canon.

Network-free heuristics over pronoun distribution and lexical cues; an LLM pass
can refine, but the deterministic classifier is what the unit tests exercise.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from app.agents.contracts import NarrativePerson

from .text_utils import strip_quotes, titlecase_names, words

_FIRST = frozenset({"i", "me", "my", "mine", "myself", "we", "us", "our", "ours"})
_SECOND = frozenset({"you", "your", "yours", "yourself", "yourselves"})
_THIRD = frozenset({
    "he", "him", "his", "she", "her", "hers", "they", "them", "their", "theirs",
})

#: Interiority verbs whose subject is the focal (POV) character in 3rd-limited.
_FOCAL_VERBS = frozenset({
    "thought", "felt", "knew", "wondered", "realized", "realised", "remembered",
    "noticed", "saw", "watched", "hoped", "feared", "decided", "wished",
    "believed", "imagined", "sensed", "understood", "recalled", "considered",
})

#: Cues that the narration is unreliable / coloured / ironic.
_UNRELIABLE_CUES = frozenset({
    "lied", "lie", "lying", "pretended", "pretend", "swore", "claimed", "supposedly",
    "allegedly", "or so", "must have", "perhaps", "maybe", "i think", "i suppose",
    "i guess", "as far as i", "if i remember", "i could have sworn", "i swear",
    "of course not", "honestly", "to be honest", "trust me", "believe me",
    "drunk", "dreamed", "imagined", "hallucination", "delirium", "feverish",
})


@dataclass(frozen=True)
class PovAnalysis:
    """A beat's narrating voice, focal character, and unreliability flag."""

    person: NarrativePerson
    focal_character: str | None
    unreliable: bool
    #: 0..1 confidence in the person classification (pronoun-share based).
    confidence: float


def classify_pov(
    text: str,
    *,
    canon_names: Mapping[str, str] | set[str] | None = None,
) -> PovAnalysis:
    """Classify the narrating person + focal character of a beat (deterministic).

    First/second/third person fall out of pronoun share in the **narration**
    (speech is stripped so a quoted "I" inside dialogue doesn't read as a
    first-person narrator). Third-person is split into *limited* (a single focal
    consciousness — interiority verbs anchored on one character) vs *omniscient*.
    """
    narration = strip_quotes(text)
    toks = words(narration)
    if not toks:
        return PovAnalysis(NarrativePerson.UNKNOWN, None, False, 0.0)

    first = sum(1 for t in toks if t in _FIRST)
    second = sum(1 for t in toks if t in _SECOND)
    third = sum(1 for t in toks if t in _THIRD)
    total_pron = first + second + third

    unreliable = _is_unreliable(text)

    # First/second person win on any meaningful share — they are marked voices.
    if first and first >= second and first >= third:
        return PovAnalysis(NarrativePerson.FIRST, None, unreliable, first / total_pron)
    if second and second >= first and second >= third:
        return PovAnalysis(NarrativePerson.SECOND, None, unreliable, second / total_pron)

    # No first/second person. If there are third-person pronouns OR named subjects
    # acting in the narration, it is third-person (a proper-name subject without
    # "I/you" reads as external/omniscient telling).
    has_named_subject = bool(titlecase_names(narration))
    if third == 0 and not has_named_subject:
        return PovAnalysis(NarrativePerson.UNKNOWN, None, unreliable, 0.0)

    focal = _focal_character(narration, canon_names)
    person = (
        NarrativePerson.THIRD_LIMITED if focal is not None else NarrativePerson.THIRD_OMNISCIENT
    )
    confidence = third / total_pron if total_pron else 0.5
    return PovAnalysis(person, focal, unreliable, confidence)


def _focal_character(
    narration: str, canon_names: Mapping[str, str] | set[str] | None
) -> str | None:
    """The character whose interiority the beat tracks, if exactly one dominates.

    Looks for "<Name> <interiority-verb>" and counts which named character is the
    subject of feeling/knowing/seeing. A single dominant subject ⇒ third-limited.
    """
    toks = narration.split()
    counts: dict[str, int] = {}
    for i, raw in enumerate(toks[:-1]):
        name = raw.strip(",.;:!?\"'")
        if not (name and name[0].isupper() and name[1:].islower()):
            continue
        nxt = toks[i + 1].strip(",.;:!?\"'").lower()
        if nxt in _FOCAL_VERBS:
            counts[name] = counts.get(name, 0) + 1
    if not counts:
        return None
    best = max(counts, key=lambda k: counts[k])
    if canon_names is not None and not _in_canon(best, canon_names):
        return None
    # Require dominance: the focal subject must account for >= half the cues.
    if counts[best] / sum(counts.values()) < 0.5:
        return None
    return best


def _in_canon(name: str, canon_names: Mapping[str, str] | set[str]) -> bool:
    cand = {name, name.lower(), name.title()}
    if isinstance(canon_names, Mapping):
        return bool(cand & set(canon_names)) or name.lower() in canon_names
    return bool(cand & set(canon_names)) or name.lower() in {n.lower() for n in canon_names}


def _is_unreliable(text: str) -> bool:
    low = text.lower()
    hits = sum(1 for cue in _UNRELIABLE_CUES if cue in low)
    return hits >= 2


def pov_changed(prev: NarrativePerson, cur: NarrativePerson) -> bool:
    """Whether the POV meaningfully switched between two consecutive beats.

    UNKNOWN is treated as "no information" — it never counts as a switch, so a
    descriptive beat between two first-person beats doesn't spuriously flip POV.
    """
    if prev is NarrativePerson.UNKNOWN or cur is NarrativePerson.UNKNOWN:
        return False
    return prev is not cur


__all__ = ["PovAnalysis", "classify_pov", "pov_changed"]
