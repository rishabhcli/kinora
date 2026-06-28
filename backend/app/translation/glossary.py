"""Glossary + do-not-translate (DNT) terminology for content translation.

A book adaptation has a fixed cast of proper nouns (character names, place
names, invented terms) and a desired rendering for domain terms. Two needs:

* **Do-not-translate (DNT):** "Elsa" must stay "Elsa" in every language — never
  translated, never transliterated by the model's whim. The canon's character
  names (§8.1) are the canonical DNT source.
* **Glossary (forced target):** a term that *should* translate but to a specific
  agreed word ("the Snow Queen" → "la Reine des neiges" in French), enforced so
  the translation is *consistent* across a 300-page book.

Both are applied by **pre-substitution + post-verification**: before translation,
DNT/glossary spans are masked exactly like markup (so the model cannot touch
them); after translation the entries are restored and verified to be present.
Matching is longest-first and (optionally) case-insensitive with case
preservation, and word-boundary-aware so "art" inside "Bart" is not matched.

The glossary is *versioned* — bumping the version invalidates dependent cached
translations (see :mod:`.hashing`), so a renamed character re-translates only the
affected segments.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .errors import GlossaryError
from .languages import canonical_tag


@dataclass(frozen=True, slots=True)
class GlossaryEntry:
    """One glossary term.

    Attributes:
        source: The source-language surface form to match.
        targets: ``target_lang → forced translation``. Empty / missing target
            means "do not translate" (the source is kept verbatim) when
            :attr:`do_not_translate` is set.
        do_not_translate: True → keep ``source`` verbatim in every language.
        case_sensitive: Match casing exactly (default: case-insensitive match
            with case preservation on the source side).
        whole_word: Require word boundaries around the match (default True).
    """

    source: str
    targets: dict[str, str] = field(default_factory=dict)
    do_not_translate: bool = False
    case_sensitive: bool = False
    whole_word: bool = True

    def target_for(self, target_lang: str) -> str | None:
        """Forced translation for a language, or None if not constrained."""
        if self.do_not_translate:
            return self.source
        canonical = canonical_tag(target_lang)
        if canonical in self.targets:
            return self.targets[canonical]
        primary = canonical.split("-", 1)[0]
        return self.targets.get(primary)


@dataclass(frozen=True, slots=True)
class GlossaryHit:
    """A glossary match against a source string."""

    entry: GlossaryEntry
    start: int
    end: int
    matched_text: str


class Glossary:
    """An ordered, versioned set of glossary / DNT entries.

    Entries are matched longest-source-first so a multi-word term ("Snow Queen")
    wins over a contained single word ("Queen"). Matching compiles one alternation
    regex per casing mode for speed; the version int is part of the cache key.
    """

    def __init__(self, entries: list[GlossaryEntry] | None = None, *, version: int = 1) -> None:
        self._entries: list[GlossaryEntry] = []
        self._version = version
        self._compiled_ci: re.Pattern[str] | None = None
        self._compiled_cs: re.Pattern[str] | None = None
        self._index_by_source: dict[str, GlossaryEntry] = {}
        for entry in entries or []:
            self.add(entry)

    @property
    def version(self) -> int:
        return self._version

    @property
    def entries(self) -> list[GlossaryEntry]:
        return list(self._entries)

    def bump_version(self) -> int:
        """Invalidate dependent caches by advancing the version. Returns new ver."""
        self._version += 1
        return self._version

    def add(self, entry: GlossaryEntry) -> None:
        """Add (or replace) an entry; recompiles the matcher lazily."""
        if not entry.source.strip():
            raise GlossaryError("glossary entry has empty source term")
        if not entry.do_not_translate and not entry.targets:
            raise GlossaryError(
                f"glossary entry {entry.source!r} is neither DNT nor has targets"
            )
        key = entry.source if entry.case_sensitive else entry.source.lower()
        # Drop any prior entry with the same source surface so add() is upsert.
        self._entries = [
            e
            for e in self._entries
            if (e.source if e.case_sensitive else e.source.lower()) != key
        ]
        self._entries.append(entry)
        self._index_by_source[key] = entry
        self._compiled_ci = None
        self._compiled_cs = None

    def _sorted(self) -> list[GlossaryEntry]:
        # Longest source first so multi-word terms win.
        return sorted(self._entries, key=lambda e: len(e.source), reverse=True)

    def find(self, text: str) -> list[GlossaryHit]:
        """Return non-overlapping glossary hits, longest-first, left-to-right."""
        hits: list[GlossaryHit] = []
        occupied: list[tuple[int, int]] = []
        for entry in self._sorted():
            pattern = self._entry_pattern(entry)
            for m in pattern.finditer(text):
                span = (m.start(), m.end())
                if any(not (span[1] <= lo or span[0] >= hi) for lo, hi in occupied):
                    continue  # overlaps an already-claimed (longer) term
                occupied.append(span)
                hits.append(GlossaryHit(entry, m.start(), m.end(), m.group(0)))
        hits.sort(key=lambda h: h.start)
        return hits

    def _entry_pattern(self, entry: GlossaryEntry) -> re.Pattern[str]:
        body = re.escape(entry.source)
        if entry.whole_word:
            body = rf"(?<!\w){body}(?!\w)"
        flags = 0 if entry.case_sensitive else re.IGNORECASE
        return re.compile(body, flags)

    def protect(self, text: str, *, target_lang: str) -> tuple[str, list[str]]:
        """Mask glossary/DNT spans before translation.

        Returns ``(masked_text, restorations)`` where each glossary span is
        replaced by a sentinel and ``restorations[i]`` is the *forced target*
        (or verbatim source for DNT) to drop back in after translation. Sentinels
        reuse the markup brackets so the same restorer handles both.
        """
        from .markup import _CLOSE, _OPEN  # reuse the markup sentinels

        hits = self.find(text)
        if not hits:
            return text, []
        out: list[str] = []
        restorations: list[str] = []
        cursor = 0
        for hit in hits:
            out.append(text[cursor : hit.start])
            forced = hit.entry.target_for(target_lang)
            replacement = forced if forced is not None else hit.matched_text
            out.append(f"{_OPEN}G{len(restorations)}{_CLOSE}")
            restorations.append(replacement)
            cursor = hit.end
        out.append(text[cursor:])
        return "".join(out), restorations

    @staticmethod
    def restore(masked_text: str, restorations: list[str]) -> str:
        """Restore glossary sentinels (``⟦G0⟧``) to their forced targets."""
        from .markup import _CLOSE, _OPEN

        pattern = re.compile(rf"{_OPEN}G(\d+){_CLOSE}")

        def _sub(match: re.Match[str]) -> str:
            idx = int(match.group(1))
            return restorations[idx] if 0 <= idx < len(restorations) else match.group(0)

        return pattern.sub(_sub, masked_text)

    def verify(self, source: str, translated: str, *, target_lang: str) -> list[str]:
        """Warn when a forced/ DNT term is missing from the translation.

        A structural consistency check independent of meaning: every glossary
        hit in the source must have its forced target present in the output.
        """
        warnings: list[str] = []
        for hit in self.find(source):
            forced = hit.entry.target_for(target_lang)
            if forced is None:
                continue
            if forced not in translated:
                kind = "DNT term" if hit.entry.do_not_translate else "glossary term"
                warnings.append(f"{kind} {forced!r} missing from translation")
        return warnings


def from_character_names(names: dict[str, str | None], *, case_sensitive: bool = True) -> Glossary:
    """Build a DNT glossary from canon character names (§8.1).

    Args:
        names: ``entity_key → display name`` (None values are skipped). Each
            name becomes a do-not-translate entry so "Elsa" survives every
            language unchanged.
    """
    entries = [
        GlossaryEntry(source=name, do_not_translate=True, case_sensitive=case_sensitive)
        for name in names.values()
        if name and name.strip()
    ]
    return Glossary(entries)


__all__ = ["Glossary", "GlossaryEntry", "GlossaryHit", "from_character_names"]
