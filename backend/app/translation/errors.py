"""Typed errors for the content-translation subsystem.

A small hierarchy so callers (the service, the API layer) can distinguish a
*bad request* (unknown language, malformed markup) from a *provider failure*
(the MT/LLM client raised) from a *quality gate* rejection. The base class is
:class:`TranslationError`; everything else is a leaf with a stable ``code`` that
the API layer maps to an HTTP problem.
"""

from __future__ import annotations


class TranslationError(Exception):
    """Base class for every translation-layer failure.

    Args:
        message: Human-readable detail (safe to surface; never contains keys).
        code: Stable machine code for the API problem mapping.
    """

    code = "translation_error"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code


class UnknownLanguageError(TranslationError):
    """A BCP-47 / language tag could not be resolved to a known language."""

    code = "unknown_language"


class MarkupError(TranslationError):
    """Placeholder/markup masking or restoration failed.

    Raised when a translated segment dropped or duplicated a protected token, or
    a placeholder closing tag has no opener — i.e. the translation corrupted the
    structure the pipeline relies on.
    """

    code = "markup_error"


class GlossaryError(TranslationError):
    """A glossary / do-not-translate definition is malformed or contradictory."""

    code = "glossary_error"


class TranslationProviderError(TranslationError):
    """The underlying MT/LLM translation client failed.

    Wraps a provider-layer exception so the service can degrade (fall back to a
    cache hit / mark a segment for review) without leaking the transport type.
    """

    code = "provider_error"


class QualityGateError(TranslationError):
    """A translation failed a hard quality gate and no fallback was available."""

    code = "quality_gate"


class ArtifactNotFoundError(TranslationError):
    """A requested persisted translation artifact does not exist."""

    code = "artifact_not_found"


class ReviewStateError(TranslationError):
    """An illegal review/post-edit state transition was requested."""

    code = "review_state_error"


__all__ = [
    "ArtifactNotFoundError",
    "GlossaryError",
    "MarkupError",
    "QualityGateError",
    "ReviewStateError",
    "TranslationError",
    "TranslationProviderError",
    "UnknownLanguageError",
]
