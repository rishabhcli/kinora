"""Markup + placeholder protection: mask → translate → restore.

Reader-facing content is not plain prose. Page text and narration scripts carry
inline markup (``<b>…</b>``, ``<i>``), templating placeholders (``{name}``,
``%s``, ``{0}``, ``%(count)d``), and structural tokens (URLs, line breaks) that
a translator must reproduce **verbatim**. An MT/LLM model left to its own devices
will happily translate ``{name}`` to ``{nom}`` or drop a ``</b>`` — corrupting
the very structure the render/UI pipeline depends on.

The standard fix (used by every serious localization toolchain) is to *mask*
each protected run with an opaque sentinel before translation and *restore* it
after:

    "Hello <b>{name}</b>"  →  "Hello ⟦0⟧{name-token}⟦1⟧"  →  translate  →  restore

The sentinels are chosen to (a) survive translation unchanged, (b) be unlikely
to occur in natural text, and (c) be order-checkable so a dropped/duplicated
token is *detected* rather than silently corrupting output. Restoration verifies
every placeholder reappears exactly once; a violation raises :class:`MarkupError`
(or, in lenient mode, records a warning and best-effort restores).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .errors import MarkupError

# Sentinel wrappers. The private-use brackets ⟦ ⟧ (U+27E6/U+27E7) are not
# something a translation model will alter, and the inner index keeps the token
# both human-readable in logs and order-checkable.
_OPEN = "⟦"  # ⟦
_CLOSE = "⟧"  # ⟧


def _sentinel(index: int) -> str:
    return f"{_OPEN}{index}{_CLOSE}"


_SENTINEL_RE = re.compile(rf"{_OPEN}(\d+){_CLOSE}")

#: Matches BOTH markup sentinels (``⟦0⟧``) and glossary sentinels (``⟦G0⟧``) —
#: any opaque protected token a translation provider must pass through verbatim.
ANY_SENTINEL_RE = re.compile(rf"{_OPEN}[A-Za-z]?\d+{_CLOSE}")

# Patterns of protected runs, in priority order (longest / most-specific first
# so e.g. an HTML tag isn't half-captured by the brace rule). Each is a
# self-contained span that must be reproduced verbatim.
_PROTECT_PATTERNS: tuple[re.Pattern[str], ...] = (
    # HTML/XML-ish tags: <b>, </b>, <span class="x">, <br/>
    re.compile(r"</?[A-Za-z][A-Za-z0-9]*(?:\s+[^<>]*?)?/?>"),
    # ICU / printf named + positional: {name}, {0}, {count, plural, ...}
    re.compile(r"\{[^{}]*\}"),
    # printf C-style: %s, %d, %1$s, %(name)s, %.2f
    re.compile(r"%(?:\(\w+\))?[#0\- +]?\d*(?:\.\d+)?[diouxXeEfFgGcrsa%]"),
    # URLs (don't translate the path/host)
    re.compile(r"https?://[^\s<>{}]+"),
    # Bare e-mail addresses
    re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"),
    # Markdown links keep their target but allow the label through: [label](url)
    # → protect only the (url) part by matching the whole and re-emitting label.
)


@dataclass(frozen=True, slots=True)
class MaskedText:
    """A masked text + the table needed to restore it.

    Attributes:
        text: The text with every protected run replaced by a sentinel.
        tokens: ``index → original run``. Restoration substitutes back.
    """

    text: str
    tokens: tuple[str, ...]

    @property
    def placeholder_count(self) -> int:
        return len(self.tokens)


def mask(text: str) -> MaskedText:
    """Replace every protected run with an order-checkable sentinel.

    Protected runs are matched left-to-right; overlapping matches are resolved by
    the leftmost-longest rule (we scan once, advancing past each capture). The
    returned :class:`MaskedText` carries the table needed by :func:`restore`.
    """
    tokens: list[str] = []
    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        best_start = n
        best_end = n
        best_match: str | None = None
        for pat in _PROTECT_PATTERNS:
            m = pat.search(text, i)
            if m is None:
                continue
            if m.start() < best_start or (m.start() == best_start and m.end() > best_end):
                best_start, best_end, best_match = m.start(), m.end(), m.group(0)
        if best_match is None:
            out.append(text[i:])
            break
        out.append(text[i:best_start])
        out.append(_sentinel(len(tokens)))
        tokens.append(best_match)
        i = best_end
    return MaskedText(text="".join(out), tokens=tuple(tokens))


def restore(masked_text: str, tokens: tuple[str, ...], *, lenient: bool = False) -> str:
    """Substitute sentinels back to their original runs.

    Verifies each sentinel index appears exactly once and is in range. In strict
    mode (default) a missing, duplicated, or out-of-range sentinel raises
    :class:`MarkupError` — that is the corruption-detection guarantee. In lenient
    mode, missing tokens are appended at the end (best-effort) and duplicates
    collapse, so the pipeline can salvage a flagged-for-review segment.

    Raises:
        MarkupError: in strict mode on any sentinel anomaly.
    """
    seen: dict[int, int] = {}

    def _sub(match: re.Match[str]) -> str:
        idx = int(match.group(1))
        seen[idx] = seen.get(idx, 0) + 1
        if idx < 0 or idx >= len(tokens):
            if lenient:
                return ""
            raise MarkupError(f"placeholder index {idx} out of range (have {len(tokens)})")
        if seen[idx] > 1 and not lenient:
            raise MarkupError(f"placeholder {idx} appeared {seen[idx]} times after translation")
        return tokens[idx]

    restored = _SENTINEL_RE.sub(_sub, masked_text)
    missing = [i for i in range(len(tokens)) if i not in seen]
    if missing:
        if not lenient:
            raise MarkupError(
                f"translation dropped {len(missing)} placeholder(s): indices {missing}"
            )
        # Lenient: append the dropped runs so no markup is lost outright.
        restored = restored + "".join(tokens[i] for i in missing)
    return restored


def placeholder_signature(text: str) -> tuple[str, ...]:
    """The multiset of protected runs in ``text`` (sorted), for equality checks.

    Two texts have the same signature iff they contain the same protected runs
    (regardless of order/position) — the basis for verifying a translation did
    not invent or drop a placeholder even when masking is bypassed.
    """
    return tuple(sorted(mask(text).tokens))


def verify_roundtrip(source: str, translated: str) -> list[str]:
    """Return human-readable warnings if ``translated`` mishandled markup.

    Compares the protected-run multisets of source and translation. Used by the
    quality layer as a structural check that is independent of meaning.
    """
    warnings: list[str] = []
    src_sig = placeholder_signature(source)
    tgt_sig = placeholder_signature(translated)
    if src_sig == tgt_sig:
        return warnings
    src_counts: dict[str, int] = {}
    tgt_counts: dict[str, int] = {}
    for tok in src_sig:
        src_counts[tok] = src_counts.get(tok, 0) + 1
    for tok in tgt_sig:
        tgt_counts[tok] = tgt_counts.get(tok, 0) + 1
    for tok, count in src_counts.items():
        delta = tgt_counts.get(tok, 0) - count
        if delta < 0:
            warnings.append(f"dropped {-delta}x markup {tok!r}")
        elif delta > 0:
            warnings.append(f"duplicated {delta}x markup {tok!r}")
    for tok in tgt_counts:
        if tok not in src_counts:
            warnings.append(f"introduced foreign markup {tok!r}")
    return warnings


__all__ = [
    "ANY_SENTINEL_RE",
    "MaskedText",
    "mask",
    "placeholder_signature",
    "restore",
    "verify_roundtrip",
]
