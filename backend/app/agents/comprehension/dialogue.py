"""Dialogue attribution + speaker diarization (§9.1 step 2, §10 no-invent).

Given a beat's raw text, this pure module:

1. extracts quoted-speech spans (:mod:`.text_utils`);
2. attributes each to a speaker, in priority order:
   a. an explicit dialogue **tag** adjacent to the quote (``"…," said Elsa`` /
      ``Elsa said, "…"``) — the strongest signal;
   b. the nearest resolvable proper name in the surrounding narration;
   c. **two-party alternation** — in a back-and-forth with two known speakers,
      an untagged line is assigned to "the other one" (the classic novel
      convention);
3. resolves the attributed name against a supplied canon name-set when given,
   demoting anything not in canon to *unattributed* (``speaker=""``) rather than
   inventing a speaker — the §10 guardrail extended to attribution.

It is fully deterministic and network-free; an LLM pass can later overwrite a
low-confidence attribution, but the heuristic alone diarizes ordinary novel
dialogue surprisingly well and keeps the unit tests fast.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

from app.agents.contracts import DialogueLine

from .text_utils import QuoteSpan, extract_quotes, titlecase_names, words

#: Verbs that mark a dialogue tag ("said", "asked", "whispered", …).
SPEECH_VERBS = frozenset({
    "said", "says", "asked", "asks", "replied", "replies", "answered", "shouted",
    "whispered", "muttered", "cried", "called", "yelled", "murmured", "exclaimed",
    "demanded", "growled", "snapped", "sighed", "laughed", "added", "continued",
    "began", "responded", "remarked", "observed", "declared", "insisted",
    "wondered", "thought", "mused", "breathed", "hissed", "stammered", "gasped",
    "interrupted", "offered", "agreed", "objected", "warned", "pleaded", "spat",
})

_VERBS = "|".join(sorted(SPEECH_VERBS, key=len, reverse=True))
_NAME = r"([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)"

# Patterns matched against the text immediately AFTER a quote (trailing tag):
#   "…," said Elsa.    → verb then name (the inverted, most common novel tag)
#   "…," Elsa said.    → name then verb
_AFTER_VERB_NAME = re.compile(rf"^[\s,—–-]*(?:{_VERBS})\s+(?:\w+ly\s+)?{_NAME}\b")
_AFTER_NAME_VERB = re.compile(rf"^[\s,—–-]*{_NAME}\s+(?:\w+ly\s+)?(?:{_VERBS})\b")
# Patterns matched against the text immediately BEFORE a quote (leading tag):
#   Elsa said, "…"     → name then verb at the end of the leading narration
#   Said Elsa, "…"     → verb then name at the end of the leading narration
_BEFORE_NAME_VERB = re.compile(rf"{_NAME}\s+(?:\w+ly\s+)?(?:{_VERBS})[\s,:—–-]*$")
_BEFORE_VERB_NAME = re.compile(rf"\b(?:{_VERBS})\s+(?:\w+ly\s+)?{_NAME}[\s,:—–-]*$")


@dataclass(frozen=True)
class Attribution:
    """A quote and the speaker the diarizer attributed it to (+ how)."""

    quote: QuoteSpan
    speaker: str
    method: str  # tag_after | tag_before | nearest_name | alternation | none
    inferred: bool


def _tag_speaker(after: str, before: str) -> tuple[str, str] | None:
    """Find an explicit dialogue-tag speaker in the text flanking a quote.

    The trailing tag is checked first (it is by far the most common novel form),
    then the leading tag. Both "said Elsa" (verb-name) and "Elsa said"
    (name-verb) orderings are recognised on each side.
    """
    m = _AFTER_VERB_NAME.match(after)
    if m:
        return m.group(1), "tag_after"
    m = _AFTER_NAME_VERB.match(after)
    if m:
        return m.group(1), "tag_after"
    m = _BEFORE_NAME_VERB.search(before)
    if m:
        return m.group(1), "tag_before"
    m = _BEFORE_VERB_NAME.search(before)
    if m:
        return m.group(1), "tag_before"
    return None


def _nearest_name(before: str, after: str) -> str | None:
    """The proper name closest to the quote (preferring the trailing tag side)."""
    after_names = titlecase_names(after[:60])
    if after_names:
        return after_names[0]
    before_names = titlecase_names(before[-60:])
    if before_names:
        return before_names[-1]
    return None


def attribute_dialogue(
    text: str,
    *,
    canon_names: Mapping[str, str] | set[str] | None = None,
) -> list[Attribution]:
    """Attribute every quoted span in ``text`` to a speaker (deterministic).

    ``canon_names`` may be a set of accepted display names or a mapping of
    normalised-name → key; when provided, an attributed name absent from it is
    dropped (no invented speakers). Two-party alternation fills untagged lines
    once at least two distinct tagged speakers are established.
    """
    quotes = extract_quotes(text)
    if not quotes:
        return []
    accepted = _accept_set(canon_names)

    # Pass 1 — explicit tags only (the strongest, unambiguous signal). The
    # narration framing each quote is clipped to the gap between this quote and
    # its neighbours so a *following* quote's words can't masquerade as a tag.
    tagged: list[Attribution] = []
    for i, span in enumerate(quotes):
        prev_end = quotes[i - 1].end if i > 0 else 0
        next_start = quotes[i + 1].start if i + 1 < len(quotes) else len(text)
        before = text[prev_end : span.start]
        after = text[span.end : next_start]
        tag = _tag_speaker(after, before)
        if tag is not None:
            tagged.append(Attribution(span, tag[0], tag[1], inferred=False))
        else:
            tagged.append(Attribution(span, "", "none", inferred=True))

    # Pass 2 — two-party alternation fills untagged lines between known speakers.
    resolved = _resolve_alternation(tagged)

    # Pass 3 — nearest-name fallback for anything still unattributed.
    out: list[Attribution] = []
    for i, attr in enumerate(resolved):
        if attr.speaker:
            out.append(attr)
            continue
        prev_end = quotes[i - 1].end if i > 0 else 0
        next_start = quotes[i + 1].start if i + 1 < len(quotes) else len(text)
        before = text[prev_end : attr.quote.start]
        after = text[attr.quote.end : next_start]
        near = _nearest_name(before, after)
        out.append(
            Attribution(attr.quote, near or "", "nearest_name" if near else "none", inferred=True)
        )

    return [_filter_canon(a, accepted) for a in out]


def _resolve_alternation(attrs: list[Attribution]) -> list[Attribution]:
    """Fill untagged lines via two-party alternation when exactly two speakers seen."""
    speakers_in_order: list[str] = [a.speaker for a in attrs if a.speaker]
    distinct = list(dict.fromkeys(speakers_in_order))
    if len(distinct) != 2:
        return attrs
    a_name, b_name = distinct
    out: list[Attribution] = []
    last: str | None = None
    for attr in attrs:
        if attr.speaker:
            last = attr.speaker
            out.append(attr)
            continue
        guess = b_name if last == a_name else a_name
        out.append(Attribution(attr.quote, guess, "alternation", inferred=True))
        last = guess
    return out


def _accept_set(canon_names: Mapping[str, str] | set[str] | None) -> set[str] | None:
    if canon_names is None:
        return None
    if isinstance(canon_names, Mapping):
        # Accept both the normalised keys and any obvious title-case display form.
        return set(canon_names) | {k.title() for k in canon_names}
    return set(canon_names) | {n.lower() for n in canon_names}


def _filter_canon(attr: Attribution, accepted: set[str] | None) -> Attribution:
    if not attr.speaker or accepted is None:
        return attr
    cand = {attr.speaker, attr.speaker.lower(), attr.speaker.title()}
    if cand & accepted:
        return attr
    # Name not in canon → don't invent a speaker; leave the line unattributed.
    return Attribution(attr.quote, "", "none", inferred=True)


def to_dialogue_lines(attrs: list[Attribution]) -> list[DialogueLine]:
    """Project diarized attributions into the contract :class:`DialogueLine` list."""
    return [
        DialogueLine(speaker=a.speaker, quote=a.quote.text, inferred=a.inferred)
        for a in attrs
    ]


def dialogue_density(text: str) -> float:
    """Fraction of a beat's words that sit inside quoted speech (0..1).

    A cheap signal the pacing pass uses: a dialogue-dense beat is a real-time
    *scene* (dense coverage) rather than narrative *summary*.
    """
    total = len(words(text))
    if total == 0:
        return 0.0
    quoted = sum(len(words(q.text)) for q in extract_quotes(text))
    return min(1.0, quoted / total)


__all__ = [
    "SPEECH_VERBS",
    "Attribution",
    "attribute_dialogue",
    "dialogue_density",
    "to_dialogue_lines",
]
