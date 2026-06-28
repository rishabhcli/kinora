"""Prompt diffing — what changed between two prompt versions, structurally.

When an operator rolls a prompt forward, the registry stores a changelog entry;
this module computes the *machine-readable* shape of that change so the changelog
can be auto-summarized and a reviewer can see exactly what moved:

* :func:`unified_text_diff` — a classic line-oriented unified diff (for display).
* :func:`token_change_stats` — added / removed / common token counts and a
  Jaccard similarity over the whitespace token sets (a cheap "how big a change").
* :func:`section_diff` — Kinora prompts are written as labelled sections
  ("GUARDRAILS:", "Return JSON ...", the ``_JSON_CONTRACT`` tail). This pulls the
  ``UPPERCASE:``-led sections out of each prompt and reports which sections were
  added / removed / changed — the diff a human actually cares about.
* :func:`suggest_bump` — a heuristic mapping a diff to a SemVer bump kind:
  a changed *guardrail* / output schema is at least MINOR; a removed section is
  MAJOR; a pure wording tweak is PATCH.

Pure module: only the stdlib + :mod:`app.llmops.semver` types. No model calls.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field

from app.llmops.semver import BumpKind

#: A "section" header is an UPPERCASE-ish label ending in a colon at line start,
#: e.g. ``GUARDRAILS:`` or ``Return JSON of the form``. We anchor on the common
#: Kinora convention: a leading capitalized word group then a colon.
_SECTION_RE = re.compile(r"^(?P<label>[A-Z][A-Za-z0-9 /&-]{1,40}):", re.MULTILINE)

#: Words whose appearance/disappearance signals a *contract* change (≥ MINOR).
_CONTRACT_WORDS = frozenset(
    {
        "guardrail",
        "guardrails",
        "json",
        "schema",
        "return",
        "negative_prompt",
        "reference_image_ids",
        "field",
        "required",
        "must",
        "never",
    }
)


def _tokens(text: str) -> list[str]:
    """Lowercased whitespace tokens with surrounding punctuation stripped."""
    return [t.strip(".,;:\"'(){}[]") for t in text.lower().split() if t.strip(".,;:\"'(){}[]")]


@dataclass(frozen=True, slots=True)
class TokenChangeStats:
    """Token-level change summary between two prompt texts."""

    added: int
    removed: int
    common: int
    jaccard: float

    @property
    def changed(self) -> int:
        """Total tokens that differ (added + removed)."""
        return self.added + self.removed

    @property
    def identical(self) -> bool:
        return self.added == 0 and self.removed == 0


def token_change_stats(old: str, new: str) -> TokenChangeStats:
    """Multiset-aware token delta + a Jaccard similarity over the token *sets*."""
    old_set = set(_tokens(old))
    new_set = set(_tokens(new))
    union = old_set | new_set
    inter = old_set & new_set
    jaccard = (len(inter) / len(union)) if union else 1.0
    return TokenChangeStats(
        added=len(new_set - old_set),
        removed=len(old_set - new_set),
        common=len(inter),
        jaccard=round(jaccard, 6),
    )


def unified_text_diff(old: str, new: str, *, old_label: str = "old", new_label: str = "new") -> str:
    """A line-oriented unified diff (empty string when the texts are identical)."""
    if old == new:
        return ""
    diff = difflib.unified_diff(
        old.splitlines(keepends=False),
        new.splitlines(keepends=False),
        fromfile=old_label,
        tofile=new_label,
        lineterm="",
    )
    return "\n".join(diff)


def _sections(text: str) -> dict[str, str]:
    """Split a prompt into ``{label: body}`` by ``Label:`` headers.

    Everything before the first header is keyed ``""`` (the preamble). Bodies run
    to the next header (or end of text).
    """
    matches = list(_SECTION_RE.finditer(text))
    out: dict[str, str] = {}
    if not matches:
        return {"": text.strip()}
    preamble = text[: matches[0].start()].strip()
    if preamble:
        out[""] = preamble
    for i, m in enumerate(matches):
        label = m.group("label").strip()
        body_start = m.end()
        body_end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        out[label] = text[body_start:body_end].strip()
    return out


@dataclass(frozen=True, slots=True)
class SectionDiff:
    """Which labelled sections were added / removed / changed between two prompts."""

    added: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    changed: tuple[str, ...] = ()
    unchanged: tuple[str, ...] = ()

    @property
    def any_change(self) -> bool:
        return bool(self.added or self.removed or self.changed)


def section_diff(old: str, new: str) -> SectionDiff:
    """Compare the labelled sections of two prompts."""
    old_sec = _sections(old)
    new_sec = _sections(new)
    added = tuple(sorted(set(new_sec) - set(old_sec)))
    removed = tuple(sorted(set(old_sec) - set(new_sec)))
    shared = set(old_sec) & set(new_sec)
    changed = tuple(sorted(k for k in shared if old_sec[k] != new_sec[k]))
    unchanged = tuple(sorted(k for k in shared if old_sec[k] == new_sec[k]))
    return SectionDiff(added=added, removed=removed, changed=changed, unchanged=unchanged)


@dataclass(frozen=True, slots=True)
class PromptDiff:
    """The full structural diff between two prompt texts."""

    identical: bool
    tokens: TokenChangeStats
    sections: SectionDiff
    unified: str = field(repr=False, default="")

    def summary(self) -> str:
        """A one-line human summary suitable for a changelog entry."""
        if self.identical:
            return "no change"
        bits: list[str] = []
        if self.sections.added:
            bits.append(f"+{len(self.sections.added)} section(s): {', '.join(self.sections.added)}")
        if self.sections.removed:
            bits.append(
                f"-{len(self.sections.removed)} section(s): {', '.join(self.sections.removed)}"
            )
        if self.sections.changed:
            bits.append(f"changed: {', '.join(self.sections.changed)}")
        bits.append(f"{self.tokens.changed} token(s) differ (jaccard {self.tokens.jaccard:.2f})")
        return "; ".join(bits)


def diff_prompts(old: str, new: str) -> PromptDiff:
    """Compute the full :class:`PromptDiff` between ``old`` and ``new``."""
    return PromptDiff(
        identical=old == new,
        tokens=token_change_stats(old, new),
        sections=section_diff(old, new),
        unified=unified_text_diff(old, new),
    )


def suggest_bump(diff: PromptDiff) -> BumpKind:
    """Heuristic SemVer bump for a diff (operator may always override).

    * **major** — a section was *removed* (a contract the downstream relied on
      disappeared);
    * **minor** — a section was *added*, or a changed section touches a contract
      word (guardrail / JSON schema / a required field);
    * **patch** — otherwise (pure wording / tone tweaks).
    """
    if diff.identical:
        return "patch"
    if diff.sections.removed:
        return "major"
    if diff.sections.added:
        return "minor"
    # A changed section that mentions a contract word is a behavioural change.
    if diff.sections.changed:
        joined = " ".join(diff.sections.changed).lower()
        if any(word in joined for word in _CONTRACT_WORDS):
            return "minor"
        # Even when the *label* is plain, a large body change is at least minor.
        if diff.tokens.changed >= 12 or diff.tokens.jaccard < 0.85:
            return "minor"
    return "patch"


__all__ = [
    "PromptDiff",
    "SectionDiff",
    "TokenChangeStats",
    "diff_prompts",
    "section_diff",
    "suggest_bump",
    "token_change_stats",
    "unified_text_diff",
]
