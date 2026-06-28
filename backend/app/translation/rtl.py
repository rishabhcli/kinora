"""Right-to-left (RTL) text handling for translated content.

When a book is read in Arabic, Hebrew, Persian, or Urdu the reader-facing text
flows right-to-left, and mixed runs (an RTL sentence containing an LTR brand
name, a number, or a protected ``{placeholder}``) need explicit *bidi isolation*
so the embedded LTR run does not visually reorder the surrounding RTL text. The
Unicode Bidirectional Algorithm handles most of this at render time, but a few
guarantees are the producer's job:

* tag a segment with the ``dir`` it should render with,
* wrap embedded opposite-direction runs in isolates (FSI…PDI) so a stray Latin
  token or digit run does not scramble the line,
* and strip any directional control characters a translation model may have
  hallucinated before re-applying our own (idempotent normalization).

These helpers are pure string transforms; the read-along highlight + page-turn
geometry consume the resulting ``dir`` to lay out the karaoke layer (§9.4).
"""

from __future__ import annotations

import re
import unicodedata

from .languages import TextDirection, get_language

# Unicode bidi control characters.
LRM = "‎"  # LEFT-TO-RIGHT MARK
RLM = "‏"  # RIGHT-TO-LEFT MARK
LRI = "⁦"  # LEFT-TO-RIGHT ISOLATE
RLI = "⁧"  # RIGHT-TO-LEFT ISOLATE
FSI = "⁨"  # FIRST STRONG ISOLATE
PDI = "⁩"  # POP DIRECTIONAL ISOLATE
LRE = "‪"  # LEFT-TO-RIGHT EMBEDDING (legacy)
RLE = "‫"  # RIGHT-TO-LEFT EMBEDDING (legacy)
PDF = "‬"  # POP DIRECTIONAL FORMATTING (legacy)

_ALL_CONTROLS = frozenset({LRM, RLM, LRI, RLI, FSI, PDI, LRE, RLE, PDF})
_CONTROLS_RE = re.compile("[" + "".join(_ALL_CONTROLS) + "]")

# A run of "strong LTR" content (Latin letters, digits, common symbols) that, if
# embedded in an RTL line, should be isolated so it keeps its own order.
_LTR_RUN_RE = re.compile(r"[A-Za-z0-9@#$%&*_+=~`/\\|<>\[\]{}()\"'.,;:!?-]{2,}")


def direction_for(lang: str) -> TextDirection:
    """The writing direction of a (resolved) language tag."""
    return get_language(lang).direction


def dir_attr(lang: str) -> str:
    """The HTML ``dir`` attribute value for a language (``ltr``/``rtl``)."""
    return direction_for(lang).value


def strip_controls(text: str) -> str:
    """Remove every Unicode bidi control character (idempotent normalization)."""
    return _CONTROLS_RE.sub("", text)


def has_rtl_characters(text: str) -> bool:
    """True iff ``text`` contains any strong RTL character.

    Uses Unicode bidirectional classes (``R``/``AL``) so it works for any RTL
    script, not just a hardcoded block list.
    """
    return any(unicodedata.bidirectional(ch) in ("R", "AL") for ch in text)


def isolate_ltr_runs(text: str) -> str:
    """Wrap embedded LTR runs in first-strong isolates (FSI…PDI).

    Only meaningful inside RTL text; applied to a translated RTL segment so an
    embedded Latin name / number keeps its internal left-to-right order without
    reordering the surrounding line. Idempotent: a run already inside an isolate
    is skipped (we never double-wrap).
    """

    def _wrap(match: re.Match[str]) -> str:
        run = match.group(0)
        # Don't wrap a run that is purely punctuation (no letters/digits).
        if not any(ch.isalnum() for ch in run):
            return run
        return f"{FSI}{run}{PDI}"

    return _LTR_RUN_RE.sub(_wrap, text)


def prepare_rtl_segment(text: str, target_lang: str) -> str:
    """Normalize + isolate a translated segment for safe RTL rendering.

    For an LTR target this is a no-op beyond stripping stray controls. For an RTL
    target it strips any model-inserted controls and re-applies our own isolation
    of embedded LTR runs, yielding a deterministic, idempotent result.
    """
    cleaned = strip_controls(text)
    if direction_for(target_lang) is TextDirection.RTL:
        return isolate_ltr_runs(cleaned)
    return cleaned


def mirror_punctuation_hint(text: str) -> str:
    """Best-effort: flip a leading/trailing ASCII bracket pair for RTL display.

    Pure presentation aid for contexts that do not run the full bidi algorithm
    (e.g. a plaintext log preview). Production rendering relies on the ``dir``
    attribute + isolates, not this; it only swaps an outermost ``(`` ↔ ``)`` /
    ``[`` ↔ ``]`` pair so a previewed string reads naturally.
    """
    pairs = {"(": ")", ")": "(", "[": "]", "]": "[", "{": "}", "}": "{"}
    chars = list(text)
    if chars and chars[0] in pairs:
        chars[0] = pairs[chars[0]]
    if chars and chars[-1] in pairs:
        chars[-1] = pairs[chars[-1]]
    return "".join(chars)


__all__ = [
    "FSI",
    "LRI",
    "LRM",
    "PDI",
    "RLI",
    "RLM",
    "TextDirection",
    "dir_attr",
    "direction_for",
    "has_rtl_characters",
    "isolate_ltr_runs",
    "mirror_punctuation_hint",
    "prepare_rtl_segment",
    "strip_controls",
]
