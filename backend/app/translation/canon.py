"""Canon integration: build a do-not-translate glossary from canon entities.

The cleanest source of proper nouns for a book is the canon itself (§8.1): every
character, location, and prop is a versioned entity with a name + aliases. Those
names must survive translation verbatim — "Elsa" is "Elsa" in every language —
so this module turns the canon entities into a :class:`Glossary` of
do-not-translate terms, merged with any persisted per-book glossary rows.

It reads the canon through a tiny injectable port (:class:`EntityNameSource`) so
the translation package never imports the canon/memory layer directly (keeping
the dependency one-way and the unit tests free of a DB). The composition root /
API layer supplies a concrete source backed by the entity repository.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Protocol

from .glossary import Glossary, GlossaryEntry


@dataclass(frozen=True, slots=True)
class CanonName:
    """A canon entity's translatable surface forms.

    Attributes:
        entity_key: The logical entity key (e.g. ``char_elsa``).
        name: The primary display name.
        aliases: Other surface forms that must also be locked ("the Snow Queen"
            when it is treated as a proper noun rather than a translatable title).
    """

    entity_key: str
    name: str
    aliases: tuple[str, ...] = ()

    def surface_forms(self) -> tuple[str, ...]:
        forms = [self.name, *self.aliases]
        # De-dupe while preserving order; drop blanks.
        seen: set[str] = set()
        out: list[str] = []
        for form in forms:
            f = form.strip()
            if f and f not in seen:
                seen.add(f)
                out.append(f)
        return tuple(out)


class EntityNameSource(Protocol):
    """An injectable read port over the canon's entity names."""

    async def character_names(self, book_id: str) -> Sequence[CanonName]:
        """Return the book's character (and other proper-noun) names."""
        ...


def glossary_from_canon_names(
    names: Iterable[CanonName],
    *,
    extra: Iterable[GlossaryEntry] | None = None,
    case_sensitive: bool = True,
    version: int = 1,
) -> Glossary:
    """Build a do-not-translate :class:`Glossary` from canon names (+ extras).

    Every surface form (name + aliases) becomes a DNT entry. Proper nouns are
    matched case-sensitively by default (so a lowercase common word that happens
    to coincide with a name is not over-protected), but the caller can relax it.
    """
    entries: list[GlossaryEntry] = []
    seen: set[str] = set()
    for canon_name in names:
        for form in canon_name.surface_forms():
            key = form if case_sensitive else form.lower()
            if key in seen:
                continue
            seen.add(key)
            entries.append(
                GlossaryEntry(
                    source=form, do_not_translate=True, case_sensitive=case_sensitive
                )
            )
    if extra:
        for entry in extra:
            entries.append(entry)
    return Glossary(entries, version=version)


async def build_book_glossary(
    source: EntityNameSource,
    book_id: str,
    *,
    extra: Iterable[GlossaryEntry] | None = None,
    version: int = 1,
) -> Glossary:
    """Async convenience: read canon names for a book and build the glossary."""
    names = await source.character_names(book_id)
    return glossary_from_canon_names(names, extra=extra, version=version)


def merge_glossaries(*glossaries: Glossary) -> Glossary:
    """Merge several glossaries; later entries win on a source-term collision.

    The merged version is the max of the inputs' versions, so a bump anywhere
    invalidates dependent caches.
    """
    merged = Glossary(version=max((g.version for g in glossaries), default=1))
    for glossary in glossaries:
        for entry in glossary.entries:
            merged.add(entry)
    return merged


__all__ = [
    "CanonName",
    "EntityNameSource",
    "build_book_glossary",
    "glossary_from_canon_names",
    "merge_glossaries",
]
