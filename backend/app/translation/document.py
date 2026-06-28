"""Document-level translation: whole pages + narration scripts, stitched back.

The service translates a *batch of segments*; this layer is the convenience on
top that turns a whole page (or a narration script) into segments, translates
them, and stitches the results back into one coherent string preserving the
original paragraph/sentence structure. It is what the rest of Kinora calls when
it has a page of text or a §9.4 narration script and wants the reader-language
version.

Two entry points:

* :meth:`DocumentTranslator.translate_page` — split a page into paragraphs →
  sentences, translate, re-join paragraphs with blank lines and sentences with
  spaces. Returns the stitched page + the per-segment results (so a caller can
  surface which sentences need review, or align word timings later).
* :meth:`DocumentTranslator.translate_narration` — same, tuned for narration
  (sentence granularity, the ``NARRATION`` content kind so the LLM provider
  keeps it speakable). Narration is stitched with single spaces so the spoken
  flow is continuous.

The stitch is structure-preserving: paragraphs detected by :mod:`.segment` are
rejoined with the blank line that separated them, so the translated page keeps
its shape for the read-along surface.
"""

from __future__ import annotations

from dataclasses import dataclass

from .languages import get_language
from .segment import segment_text, split_paragraphs, split_sentences
from .service import TranslationService
from .types import (
    ContentKind,
    Segment,
    TranslatedSegment,
    TranslationRequest,
)


@dataclass(frozen=True, slots=True)
class TranslatedDocument:
    """The result of translating a whole page / narration script."""

    text: str
    source_lang: str
    target_lang: str
    rtl: bool
    segments: tuple[TranslatedSegment, ...]
    review_count: int

    @property
    def needs_review(self) -> bool:
        return self.review_count > 0


class DocumentTranslator:
    """Page/narration-level translation on top of a :class:`TranslationService`."""

    def __init__(self, service: TranslationService) -> None:
        self._service = service

    async def translate_page(
        self,
        text: str,
        *,
        book_id: str,
        target_lang: str,
        source_lang: str | None = None,
        base_id: str = "page",
        back_translate: bool = False,
    ) -> TranslatedDocument:
        """Translate a page, preserving its paragraph/sentence structure."""
        return await self._translate_structured(
            text,
            book_id=book_id,
            target_lang=target_lang,
            source_lang=source_lang,
            base_id=base_id,
            kind=ContentKind.PAGE_TEXT,
            back_translate=back_translate,
            paragraph_join="\n\n",
            sentence_join=" ",
        )

    async def translate_narration(
        self,
        text: str,
        *,
        book_id: str,
        target_lang: str,
        source_lang: str | None = None,
        base_id: str = "narration",
        back_translate: bool = False,
    ) -> TranslatedDocument:
        """Translate a narration script (sentence granularity, speakable kind)."""
        return await self._translate_structured(
            text,
            book_id=book_id,
            target_lang=target_lang,
            source_lang=source_lang,
            base_id=base_id,
            kind=ContentKind.NARRATION,
            back_translate=back_translate,
            paragraph_join=" ",
            sentence_join=" ",
        )

    async def _translate_structured(
        self,
        text: str,
        *,
        book_id: str,
        target_lang: str,
        source_lang: str | None,
        base_id: str,
        kind: ContentKind,
        back_translate: bool,
        paragraph_join: str,
        sentence_join: str,
    ) -> TranslatedDocument:
        # Detect the source language once so sentence-splitting uses the right
        # terminators (CJK/Arabic punctuation differs).
        detect_lang = source_lang or self._service.detect_source((Segment(id="x", text=text),))
        paragraphs = split_paragraphs(text)

        # Build segments with ids that encode (paragraph, sentence) so we can
        # reconstruct the structure after translation.
        segments: list[Segment] = []
        structure: list[list[str]] = []  # [paragraph][sentence_seg_id]
        for p_idx, para in enumerate(paragraphs):
            sentence_ids: list[str] = []
            sentences = split_sentences(para, detect_lang)
            for s_idx, sentence in enumerate(sentences):
                seg_id = f"{base_id}.{p_idx}.{s_idx}"
                segments.append(Segment(id=seg_id, text=sentence, kind=kind))
                sentence_ids.append(seg_id)
            structure.append(sentence_ids)

        if not segments:
            lang = get_language(target_lang)
            return TranslatedDocument(
                text="",
                source_lang=detect_lang,
                target_lang=lang.tag,
                rtl=lang.is_rtl,
                segments=(),
                review_count=0,
            )

        result = await self._service.translate(
            TranslationRequest(
                book_id=book_id,
                target_lang=target_lang,
                segments=tuple(segments),
                source_lang=source_lang,
                back_translate=back_translate,
            )
        )
        by_id = result.by_id()

        # Stitch: sentences within a paragraph, paragraphs with the structural join.
        translated_paragraphs: list[str] = []
        for sentence_ids in structure:
            sentences = [by_id[sid].translated_text for sid in sentence_ids if sid in by_id]
            translated_paragraphs.append(sentence_join.join(sentences))
        stitched = paragraph_join.join(p for p in translated_paragraphs if p)

        return TranslatedDocument(
            text=stitched,
            source_lang=result.source_lang,
            target_lang=result.target_lang,
            rtl=result.rtl,
            segments=result.segments,
            review_count=result.review_count,
        )

    async def translate_entity_description(
        self,
        description: str,
        *,
        book_id: str,
        target_lang: str,
        source_lang: str | None = None,
        entity_key: str = "entity",
    ) -> TranslatedDocument:
        """Translate a canon entity description (terse, ``ENTITY_DESCRIPTION`` kind)."""
        segments = segment_text(
            description,
            base_id=f"desc.{entity_key}",
            kind=ContentKind.ENTITY_DESCRIPTION,
            granularity="sentence",
        )
        if not segments:
            lang = get_language(target_lang)
            return TranslatedDocument("", source_lang or "en", lang.tag, lang.is_rtl, (), 0)
        result = await self._service.translate(
            TranslationRequest(
                book_id=book_id,
                target_lang=target_lang,
                segments=tuple(segments),
                source_lang=source_lang,
            )
        )
        by_id = result.by_id()
        stitched = " ".join(
            by_id[s.id].translated_text for s in segments if s.id in by_id
        )
        return TranslatedDocument(
            text=stitched,
            source_lang=result.source_lang,
            target_lang=result.target_lang,
            rtl=result.rtl,
            segments=result.segments,
            review_count=result.review_count,
        )


__all__ = ["DocumentTranslator", "TranslatedDocument"]
