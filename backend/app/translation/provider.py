"""The translation-provider abstraction + a deterministic test double.

Every machine-translation call goes through :class:`TranslationProvider` so the
real LLM/MT client is an injectable seam. The service never imports a transport;
it holds a provider. In tests the provider is :class:`FakeTranslationProvider`,
which is pure and deterministic — **zero live calls, zero credits** — yet
exercises the whole pipeline (markup survival, glossary, RTL, cost accounting,
quality, review) because it is shaped exactly like the real one.

A provider receives a *batch* of already-masked source strings (the service does
markup/glossary masking) and returns target strings 1:1, plus a
:class:`~app.translation.types.TranslationCost` for the call. Working on masked
strings is what lets the fake guarantee placeholder survival without
understanding markup: it never touches a sentinel.
"""

from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from dataclasses import dataclass

from .languages import get_language, same_language
from .markup import ANY_SENTINEL_RE
from .types import ContentKind, TranslationCost


@dataclass(frozen=True, slots=True)
class ProviderRequest:
    """One unit handed to a provider (already markup/glossary-masked)."""

    masked_text: str
    source_lang: str
    target_lang: str
    kind: ContentKind = ContentKind.PAGE_TEXT
    context: str | None = None


@dataclass(frozen=True, slots=True)
class ProviderResponse:
    """A provider's output for a batch."""

    texts: tuple[str, ...]
    cost: TranslationCost


class TranslationProvider(ABC):
    """Async machine-translation seam.

    Implementations MUST preserve any ``⟦n⟧`` sentinel substrings verbatim and
    return exactly one output per input, in order.
    """

    name: str = "abstract"

    @abstractmethod
    async def translate_batch(self, requests: list[ProviderRequest]) -> ProviderResponse:
        """Translate a batch of masked strings, preserving sentinels in order."""
        raise NotImplementedError

    async def back_translate(self, requests: list[ProviderRequest]) -> ProviderResponse:
        """Translate target→source for a round-trip quality check.

        Default implementation swaps the languages and reuses
        :meth:`translate_batch`; an LLM provider may override with a dedicated
        prompt. Used by the quality layer (§ back-translation check).
        """
        swapped = [
            ProviderRequest(
                masked_text=r.masked_text,
                source_lang=r.target_lang,
                target_lang=r.source_lang,
                kind=r.kind,
                context=r.context,
            )
            for r in requests
        ]
        return await self.translate_batch(swapped)


class FakeTranslationProvider(TranslationProvider):
    """A deterministic, dependency-free provider for tests + local runs.

    It does not understand language; it *simulates* translation in a way that is
    (a) deterministic, (b) sentinel-preserving, (c) reversible enough for a
    back-translation round-trip to look plausible, and (d) cost-accounted. The
    transform tags each real word with a short language-derived suffix so the
    output is visibly "translated" and differs per target language, while
    sentinels pass through untouched.

    Set ``identity_languages`` to a set of pairs you want passed through verbatim
    (used to exercise the passthrough path), or ``corrupt_markup=True`` to make
    the fake *drop* a sentinel (used to exercise markup-failure handling).
    """

    name = "fake"

    def __init__(
        self,
        *,
        corrupt_markup: bool = False,
        drop_glossary: bool = False,
        latency_tokens: int = 8,
    ) -> None:
        self._corrupt = corrupt_markup
        self._drop_glossary = drop_glossary
        self._latency_tokens = latency_tokens
        #: Number of batches served (test introspection).
        self.calls = 0
        #: Every masked input seen, for assertions.
        self.seen: list[str] = []

    async def translate_batch(self, requests: list[ProviderRequest]) -> ProviderResponse:
        self.calls += 1
        out: list[str] = []
        in_tokens = 0
        out_tokens = 0
        for req in requests:
            self.seen.append(req.masked_text)
            in_tokens += _token_estimate(req.masked_text)
            translated = self._transform(req.masked_text, req.source_lang, req.target_lang)
            out_tokens += _token_estimate(translated)
            out.append(translated)
        cost = TranslationCost(
            input_tokens=in_tokens,
            output_tokens=out_tokens,
            provider_calls=1,
            segments=len(requests),
        )
        return ProviderResponse(texts=tuple(out), cost=cost)

    def _transform(self, text: str, source_lang: str, target_lang: str) -> str:
        if same_language(source_lang, target_lang):
            return text
        suffix = _lang_suffix(target_lang)
        # Walk the text, translating real chunks and passing sentinels through
        # verbatim. ``finditer`` gives us the exact sentinel spans, so the
        # interleaving is unambiguous (a capturing ``split`` would double the
        # index text and corrupt the round-trip).
        rebuilt: list[str] = []
        cursor = 0
        dropped_one = False
        for match in ANY_SENTINEL_RE.finditer(text):
            rebuilt.append(self._translate_chunk(text[cursor : match.start()], suffix))
            if self._corrupt and not dropped_one:
                dropped_one = True  # simulate a model dropping a placeholder
            else:
                rebuilt.append(match.group(0))
            cursor = match.end()
        rebuilt.append(self._translate_chunk(text[cursor:], suffix))
        result = "".join(rebuilt)
        if self._drop_glossary:
            # Remove a glossary sentinel restoration target if present.
            result = result.replace("⟦G0⟧", "")
        return result

    @staticmethod
    def _translate_chunk(chunk: str, suffix: str) -> str:
        if not chunk.strip():
            return chunk
        words = chunk.split(" ")
        out: list[str] = []
        for w in words:
            if not w or not any(c.isalnum() for c in w):
                out.append(w)
                continue
            out.append(f"{w}{suffix}")
        return " ".join(out)

    async def back_translate(self, requests: list[ProviderRequest]) -> ProviderResponse:
        """Strip the deterministic suffix to recover something near the source."""
        self.calls += 1
        out: list[str] = []
        in_tokens = 0
        out_tokens = 0
        for req in requests:
            in_tokens += _token_estimate(req.masked_text)
            suffix = _lang_suffix(req.source_lang)  # source_lang here is the *target* of fwd
            recovered = req.masked_text.replace(suffix, "")
            out_tokens += _token_estimate(recovered)
            out.append(recovered)
        cost = TranslationCost(
            input_tokens=in_tokens,
            output_tokens=out_tokens,
            provider_calls=1,
            segments=len(requests),
        )
        return ProviderResponse(texts=tuple(out), cost=cost)


def _lang_suffix(target_lang: str) -> str:
    """A short deterministic per-language suffix the fake appends to words."""
    primary = get_language(target_lang).primary_subtag
    return f"·{primary}"


def _token_estimate(text: str) -> int:
    """Cheap token estimate (≈ ¾ word) so the fake's cost is non-trivial."""
    words = max(len(text.split()), 1)
    # Stable, deterministic, monotonic in length.
    return words + int(hashlib.sha1(text.encode("utf-8")).hexdigest(), 16) % 3


__all__ = [
    "FakeTranslationProvider",
    "ProviderRequest",
    "ProviderResponse",
    "TranslationProvider",
]
