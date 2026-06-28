"""Translation memory (TM): the cache that makes a re-translation free (§8.7).

A translation memory is the localization industry's name for "what we already
translated." It is the §8.7 idea applied to text: a segment translated once into
a language is stored keyed by its content hash, and a later request for the same
source+language+glossary is an *exact match* served at zero provider cost.

Beyond exact matches, a TM offers **fuzzy matches**: a segment that is *almost*
the same as a stored one (a typo fixed, a comma added) can reuse the prior
translation as a high-confidence suggestion instead of paying for a fresh call.
We score fuzziness with a normalized Levenshtein ratio over the *masked* source
(so a placeholder edit doesn't tank the score) and only suggest above a
threshold.

:class:`TranslationMemory` is the in-process store (used directly in tests and as
a hot cache in front of the DB). The persisted artifacts (DB) are the durable
backing store; the service reads the DB into the TM and writes back.
"""

from __future__ import annotations

from dataclasses import dataclass

from .hashing import translation_key
from .markup import mask
from .types import ContentKind


@dataclass(frozen=True, slots=True)
class MemoryEntry:
    """One stored translation unit."""

    source_text: str
    translated_text: str
    source_lang: str
    target_lang: str
    content_kind: ContentKind
    glossary_version: int
    quality: float = 1.0

    @property
    def key(self) -> str:
        return translation_key(
            source_text=self.source_text,
            source_lang=self.source_lang,
            target_lang=self.target_lang,
            content_kind=self.content_kind,
            glossary_version=self.glossary_version,
        )


@dataclass(frozen=True, slots=True)
class FuzzyMatch:
    """A near (not exact) memory hit."""

    entry: MemoryEntry
    ratio: float  # similarity in [0, 1]


def levenshtein(a: str, b: str) -> int:
    """Classic edit distance (insert/delete/substitute), O(len(a)*len(b)) space-optimal."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    # Keep the shorter string as the inner loop for a smaller row.
    if len(a) < len(b):
        a, b = b, a
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        current = [i]
        for j, cb in enumerate(b, 1):
            cost = 0 if ca == cb else 1
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + cost))
        previous = current
    return previous[-1]


def similarity_ratio(a: str, b: str) -> float:
    """Normalized similarity in ``[0, 1]`` (1 = identical), from edit distance."""
    if not a and not b:
        return 1.0
    distance = levenshtein(a, b)
    longest = max(len(a), len(b))
    return 1.0 - distance / longest if longest else 1.0


class TranslationMemory:
    """In-process exact + fuzzy translation cache.

    Exact lookup is O(1) by content hash. Fuzzy lookup scans the entries that
    share ``(source_lang, target_lang, content_kind, glossary_version)`` — a
    small candidate set in practice (one book × one language) — and returns the
    best match above ``fuzzy_threshold``.
    """

    def __init__(self, *, fuzzy_threshold: float = 0.82) -> None:
        self._by_key: dict[str, MemoryEntry] = {}
        # Bucketed for fuzzy scanning: (src, tgt, kind, gver) → list of entries.
        self._buckets: dict[tuple[str, str, str, int], list[MemoryEntry]] = {}
        self._fuzzy_threshold = fuzzy_threshold

    def __len__(self) -> int:
        return len(self._by_key)

    @property
    def fuzzy_threshold(self) -> float:
        return self._fuzzy_threshold

    def put(self, entry: MemoryEntry) -> None:
        """Store (or overwrite) an entry."""
        self._by_key[entry.key] = entry
        bucket_key = (
            entry.source_lang,
            entry.target_lang,
            entry.content_kind.value,
            entry.glossary_version,
        )
        bucket = self._buckets.setdefault(bucket_key, [])
        # Replace a same-source entry in the bucket to avoid growth on re-put.
        for i, existing in enumerate(bucket):
            if existing.source_text == entry.source_text:
                bucket[i] = entry
                return
        bucket.append(entry)

    def get_exact(
        self,
        *,
        source_text: str,
        source_lang: str,
        target_lang: str,
        content_kind: ContentKind,
        glossary_version: int,
    ) -> MemoryEntry | None:
        """Exact content-hash lookup (zero-cost cache hit)."""
        key = translation_key(
            source_text=source_text,
            source_lang=source_lang,
            target_lang=target_lang,
            content_kind=content_kind,
            glossary_version=glossary_version,
        )
        return self._by_key.get(key)

    def get_fuzzy(
        self,
        *,
        source_text: str,
        source_lang: str,
        target_lang: str,
        content_kind: ContentKind,
        glossary_version: int,
    ) -> FuzzyMatch | None:
        """Best near-match above the threshold, or None.

        Compares the *masked* source so a placeholder/markup difference does not
        dominate the score — only the translatable prose matters for reuse.
        """
        bucket = self._buckets.get(
            (source_lang, target_lang, content_kind.value, glossary_version)
        )
        if not bucket:
            return None
        query = mask(source_text).text
        best: FuzzyMatch | None = None
        for entry in bucket:
            if entry.source_text == source_text:
                continue  # exact would have been served already
            ratio = similarity_ratio(query, mask(entry.source_text).text)
            if ratio >= self._fuzzy_threshold and (best is None or ratio > best.ratio):
                best = FuzzyMatch(entry, ratio)
        return best

    def clear(self) -> None:
        self._by_key.clear()
        self._buckets.clear()


__all__ = [
    "FuzzyMatch",
    "MemoryEntry",
    "TranslationMemory",
    "levenshtein",
    "similarity_ratio",
]
