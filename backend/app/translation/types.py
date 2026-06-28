"""Shared value types for the content-translation subsystem.

These are the data the pipeline passes between layers: a :class:`Segment` of
source content, a :class:`TranslatedSegment` result carrying provenance + a
quality score, the :class:`TranslationRequest`/:class:`TranslationResult`
envelope the service speaks, and the :class:`TranslationCost` accounting unit.

They are deliberately framework-light dataclasses (not ORM rows) so the pure
pipeline layers stay testable without a database. Persistence (artifacts.py)
maps them onto SQLAlchemy models.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class ContentKind(StrEnum):
    """What kind of reader-facing content a segment is.

    The kind tunes the prompt the LLM provider builds (narration must stay
    speakable and keep its word rhythm; an entity description is terse), and it
    namespaces the content hash so a page string and an identically-worded
    narration line do not collide in the cache.
    """

    PAGE_TEXT = "page_text"
    ENTITY_DESCRIPTION = "entity_description"
    NARRATION = "narration"
    UI_FALLBACK = "ui_fallback"


class TranslationOrigin(StrEnum):
    """Where a translated segment's text came from (its provenance)."""

    MEMORY = "memory"  # served from translation memory / cache — zero cost
    PROVIDER = "provider"  # freshly produced by the MT/LLM provider
    GLOSSARY = "glossary"  # the whole segment was a do-not-translate term
    PASSTHROUGH = "passthrough"  # source == target language; no work needed
    POST_EDIT = "post_edit"  # a human reviewer replaced the machine output


@dataclass(frozen=True, slots=True)
class Segment:
    """One unit of source content to translate.

    Attributes:
        id: Caller-supplied stable id (e.g. ``page_12.para_3`` or a beat id).
            Echoed back on the result so callers can re-associate.
        text: The source text (may contain markup / placeholders).
        kind: What this content is (tunes prompting + hash namespace).
        context: Optional surrounding text to disambiguate (not translated).
        metadata: Opaque passthrough (page number, entity key, …).
    """

    id: str
    text: str
    kind: ContentKind = ContentKind.PAGE_TEXT
    context: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class TranslatedSegment:
    """The translation of one :class:`Segment`.

    Attributes:
        id: The source segment id (echoed).
        source_text: The original text.
        translated_text: The target-language text (markup restored).
        source_lang: Resolved canonical source tag.
        target_lang: Resolved canonical target tag.
        origin: Provenance (memory / provider / …).
        quality: Quality estimate in ``[0, 1]`` (1 = confident).
        needs_review: True iff quality fell below the review threshold or a
            hard check (markup/placeholder/glossary) emitted a warning.
        warnings: Non-fatal issues detected during the round-trip.
        source_hash: The content hash this translation is keyed to (§8.7-style).
    """

    id: str
    source_text: str
    translated_text: str
    source_lang: str
    target_lang: str
    origin: TranslationOrigin
    quality: float = 1.0
    needs_review: bool = False
    warnings: tuple[str, ...] = ()
    source_hash: str = ""


@dataclass(frozen=True, slots=True)
class TranslationRequest:
    """A batch translation request handed to the service."""

    book_id: str
    target_lang: str
    segments: tuple[Segment, ...]
    source_lang: str | None = None  # None → detect
    back_translate: bool = False
    use_memory: bool = True
    persist: bool = True


@dataclass(frozen=True, slots=True)
class TranslationCost:
    """Cost accounting for a translation batch.

    ``video_seconds`` is always 0 (this subsystem never renders video); it is
    present only so the unit lines up structurally with the provider layer's
    :class:`~app.providers.types.Usage` for an eventual budget cross-walk.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    provider_calls: int = 0
    cache_hits: int = 0
    segments: int = 0
    video_seconds: float = 0.0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def cache_hit_rate(self) -> float:
        """Fraction of segments served from memory (0 when no segments)."""
        return self.cache_hits / self.segments if self.segments else 0.0

    def merge(self, other: TranslationCost) -> TranslationCost:
        """Sum two cost units (for accumulating across batches)."""
        return TranslationCost(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            provider_calls=self.provider_calls + other.provider_calls,
            cache_hits=self.cache_hits + other.cache_hits,
            segments=self.segments + other.segments,
            video_seconds=self.video_seconds + other.video_seconds,
        )


@dataclass(frozen=True, slots=True)
class TranslationResult:
    """The service's response to a :class:`TranslationRequest`."""

    book_id: str
    source_lang: str
    target_lang: str
    segments: tuple[TranslatedSegment, ...]
    cost: TranslationCost
    rtl: bool = False

    @property
    def review_count(self) -> int:
        """How many segments were flagged for human post-edit."""
        return sum(1 for s in self.segments if s.needs_review)

    def by_id(self) -> dict[str, TranslatedSegment]:
        """Index the results by source segment id."""
        return {s.id: s for s in self.segments}


__all__ = [
    "ContentKind",
    "Segment",
    "TranslatedSegment",
    "TranslationCost",
    "TranslationOrigin",
    "TranslationRequest",
    "TranslationResult",
]
