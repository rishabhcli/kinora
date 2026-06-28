"""Content-translation subsystem (kinora.md §8, §9).

Translates the reader-facing material of a book adaptation — page text, canon
entity descriptions, and narration scripts — into a reader's language while
preserving markup/placeholders, locking proper nouns (do-not-translate), enforcing
glossary terms, handling RTL scripts, estimating quality (with back-translation),
caching against source-content hashes (so a re-read costs nothing, §8.7), and
flagging low-confidence segments for human post-edit.

This is **distinct** from any UI-string i18n layer: it translates *content*, not
button labels. It never renders video and makes no live model calls in tests —
every MT/LLM call is behind :class:`~app.translation.provider.TranslationProvider`
with a deterministic :class:`~app.translation.provider.FakeTranslationProvider`.

The public entry point is :class:`~app.translation.service.TranslationService`.
"""

from __future__ import annotations

from .canon import CanonName, build_book_glossary, glossary_from_canon_names, merge_glossaries
from .document import DocumentTranslator, TranslatedDocument
from .errors import (
    GlossaryError,
    MarkupError,
    QualityGateError,
    TranslationError,
    TranslationProviderError,
    UnknownLanguageError,
)
from .glossary import Glossary, GlossaryEntry
from .languages import Language, TextDirection, get_language, is_rtl, supported_languages
from .provider import (
    FakeTranslationProvider,
    ProviderRequest,
    ProviderResponse,
    TranslationProvider,
)
from .types import (
    ContentKind,
    Segment,
    TranslatedSegment,
    TranslationCost,
    TranslationOrigin,
    TranslationRequest,
    TranslationResult,
)

__all__ = [
    "CanonName",
    "ContentKind",
    "DocumentTranslator",
    "FakeTranslationProvider",
    "Glossary",
    "GlossaryEntry",
    "GlossaryError",
    "Language",
    "MarkupError",
    "ProviderRequest",
    "ProviderResponse",
    "QualityGateError",
    "Segment",
    "TextDirection",
    "TranslatedDocument",
    "TranslatedSegment",
    "TranslationCost",
    "TranslationError",
    "TranslationOrigin",
    "TranslationProvider",
    "TranslationProviderError",
    "TranslationRequest",
    "TranslationResult",
    "UnknownLanguageError",
    "build_book_glossary",
    "get_language",
    "glossary_from_canon_names",
    "is_rtl",
    "merge_glossaries",
    "supported_languages",
]
