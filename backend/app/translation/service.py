"""TranslationService — the orchestrator that wires the whole pipeline.

This is the public entry point. It takes a :class:`TranslationRequest` (a book
id, a target language, and a batch of source :class:`Segment` s) and runs the
pipeline described in DESIGN.md:

    detect source language (if unknown)
      → per segment: mask markup + glossary, TM lookup, provider on miss,
        restore, RTL-prepare, quality-estimate (+ optional back-translation)
      → flag low-confidence segments for review
      → return a TranslationResult with full cost accounting

The service is pure-ish: its only collaborators are injected — a
:class:`~app.translation.provider.TranslationProvider`, an optional
:class:`~app.translation.glossary.Glossary`, an optional
:class:`~app.translation.memory_store.TranslationMemory`, and an optional
:class:`Detector`. With the :class:`FakeTranslationProvider` and an in-memory TM
it runs end-to-end in tests with **zero live calls**. Persistence is handled by
the API/composition layer feeding the TM from the DB and writing results back
(see :mod:`.artifacts`); the service itself does no I/O.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.logging import get_logger

from .cost import CostLedger, batch_requests
from .detect import Detector, default_detector
from .errors import TranslationProviderError
from .glossary import Glossary
from .languages import canonical_tag, get_language, same_language
from .markup import mask, restore
from .memory_store import MemoryEntry, TranslationMemory
from .provider import ProviderRequest, TranslationProvider
from .quality import estimate_quality
from .rtl import prepare_rtl_segment
from .types import (
    ContentKind,
    Segment,
    TranslatedSegment,
    TranslationCost,
    TranslationOrigin,
    TranslationRequest,
    TranslationResult,
)

logger = get_logger("app.translation.service")


@dataclass(frozen=True, slots=True)
class _Prepared:
    """Internal: a segment after masking, before/awaiting provider translation."""

    segment: Segment
    masked_text: str
    markup_tokens: tuple[str, ...]
    glossary_restorations: tuple[str, ...]
    request: ProviderRequest


class TranslationService:
    """Orchestrates segment-aware content translation (DESIGN.md pipeline)."""

    def __init__(
        self,
        provider: TranslationProvider,
        *,
        glossary: Glossary | None = None,
        memory: TranslationMemory | None = None,
        detector: Detector | None = None,
        review_threshold: float = 0.7,
    ) -> None:
        self._provider = provider
        self._glossary = glossary
        self._memory = memory or TranslationMemory()
        self._detector = detector or default_detector
        self._review_threshold = review_threshold

    @property
    def memory(self) -> TranslationMemory:
        return self._memory

    @property
    def glossary_version(self) -> int:
        return self._glossary.version if self._glossary is not None else 0

    # -- fuzzy suggestions (translation-memory leverage) ------------------ #

    def fuzzy_suggestion(
        self, text: str, *, source_lang: str, target_lang: str, kind: ContentKind
    ) -> tuple[str, float] | None:
        """Return a near (not exact) prior translation + its similarity, or None.

        A fuzzy TM hit is a *suggestion*, not an auto-applied translation: the
        prose is almost identical to a stored segment (a typo fixed, a comma
        added), so the prior translation is a high-confidence starting point a
        reviewer can accept. The service deliberately does not auto-apply it (only
        exact hits are free); this surfaces the capability for a review/post-edit
        UI without guessing on the reader's behalf.
        """
        match = self._memory.get_fuzzy(
            source_text=text,
            source_lang=canonical_tag(source_lang),
            target_lang=canonical_tag(target_lang),
            content_kind=kind,
            glossary_version=self.glossary_version,
        )
        if match is None:
            return None
        return (match.entry.translated_text, match.ratio)

    # -- detection -------------------------------------------------------- #

    def detect_source(self, segments: tuple[Segment, ...], *, default: str = "en") -> str:
        """Detect the source language from a representative sample of segments.

        Concatenates the longest few segments (more signal) and runs the
        detector once, returning a canonical tag.
        """
        sample = " ".join(
            sorted((s.text for s in segments), key=len, reverse=True)[:5]
        )
        if not sample.strip():
            return canonical_tag(default)
        detection = self._detector.detect(sample, default=default)
        return detection.language.tag

    # -- the pipeline ----------------------------------------------------- #

    async def translate(self, request: TranslationRequest) -> TranslationResult:
        """Run the full pipeline for a batch request."""
        target = canonical_tag(request.target_lang)
        source = (
            canonical_tag(request.source_lang)
            if request.source_lang
            else self.detect_source(request.segments)
        )
        target_lang = get_language(target)
        ledger = CostLedger()

        # Passthrough: same language → no work, but still return structured
        # results so callers don't special-case.
        if same_language(source, target):
            passthrough = tuple(
                TranslatedSegment(
                    id=s.id,
                    source_text=s.text,
                    translated_text=s.text,
                    source_lang=source,
                    target_lang=target,
                    origin=TranslationOrigin.PASSTHROUGH,
                    quality=1.0,
                )
                for s in request.segments
            )
            return TranslationResult(
                book_id=request.book_id,
                source_lang=source,
                target_lang=target,
                segments=passthrough,
                cost=TranslationCost(segments=len(passthrough)),
                rtl=target_lang.is_rtl,
            )

        # 1) Resolve cache hits + prepare provider requests for the misses.
        cached: dict[str, TranslatedSegment] = {}
        prepared: list[_Prepared] = []
        for seg in request.segments:
            hit = self._lookup(seg, source, target) if request.use_memory else None
            if hit is not None:
                cached[seg.id] = hit
                ledger.record_cache_hit(target_lang=target, content_kind=seg.kind)
                continue
            prepared.append(self._prepare(seg, source, target))

        # 2) Translate the misses in batches.
        translated_by_id: dict[str, str] = {}
        if prepared:
            translated_by_id = await self._run_provider(prepared, ledger, target=target)

        # 3) Restore, RTL-prepare, quality-gate, (optionally) back-translate.
        fresh = await self._finalize(
            prepared, translated_by_id, source=source, target=target, ledger=ledger,
            back_translate=request.back_translate,
        )

        # 4) Assemble in original order; write fresh translations to memory.
        results: list[TranslatedSegment] = []
        for seg in request.segments:
            if seg.id in cached:
                results.append(cached[seg.id])
            else:
                ts = fresh[seg.id]
                results.append(ts)
                if request.use_memory and not ts.needs_review:
                    self._memory.put(
                        MemoryEntry(
                            source_text=seg.text,
                            translated_text=ts.translated_text,
                            source_lang=source,
                            target_lang=target,
                            content_kind=seg.kind,
                            glossary_version=self.glossary_version,
                            quality=ts.quality,
                        )
                    )

        logger.info(
            "translation.batch",
            book_id=request.book_id,
            source=source,
            target=target,
            segments=len(results),
            cache_hits=ledger.total.cache_hits,
            provider_calls=ledger.total.provider_calls,
            review=sum(1 for r in results if r.needs_review),
        )
        return TranslationResult(
            book_id=request.book_id,
            source_lang=source,
            target_lang=target,
            segments=tuple(results),
            cost=ledger.total,
            rtl=target_lang.is_rtl,
        )

    # -- steps ------------------------------------------------------------ #

    def _lookup(self, seg: Segment, source: str, target: str) -> TranslatedSegment | None:
        entry = self._memory.get_exact(
            source_text=seg.text,
            source_lang=source,
            target_lang=target,
            content_kind=seg.kind,
            glossary_version=self.glossary_version,
        )
        if entry is None:
            return None
        return TranslatedSegment(
            id=seg.id,
            source_text=seg.text,
            translated_text=entry.translated_text,
            source_lang=source,
            target_lang=target,
            origin=TranslationOrigin.MEMORY,
            quality=entry.quality,
        )

    def _prepare(self, seg: Segment, source: str, target: str) -> _Prepared:
        # Glossary/DNT masking first (so its sentinels are protected), then
        # markup masking over the result.
        glossary_restorations: tuple[str, ...] = ()
        text = seg.text
        if self._glossary is not None:
            text, restorations = self._glossary.protect(text, target_lang=target)
            glossary_restorations = tuple(restorations)
        masked = mask(text)
        request = ProviderRequest(
            masked_text=masked.text,
            source_lang=source,
            target_lang=target,
            kind=seg.kind,
            context=seg.context,
        )
        return _Prepared(
            segment=seg,
            masked_text=masked.text,
            markup_tokens=masked.tokens,
            glossary_restorations=glossary_restorations,
            request=request,
        )

    async def _run_provider(
        self, prepared: list[_Prepared], ledger: CostLedger, *, target: str
    ) -> dict[str, str]:
        """Translate prepared misses in batches; return ``seg_id → masked output``."""
        batches = batch_requests([p.request for p in prepared])
        # Map flat request order back to segment ids.
        flat_ids = [p.segment.id for p in prepared]
        out: dict[str, str] = {}
        cursor = 0
        for batch in batches:
            try:
                response = await self._provider.translate_batch(batch)
            except Exception as exc:  # noqa: BLE001 - normalize transport faults
                raise TranslationProviderError(
                    f"translation provider {self._provider.name!r} failed: {exc}"
                ) from exc
            if len(response.texts) != len(batch):
                raise TranslationProviderError(
                    f"provider returned {len(response.texts)} outputs for {len(batch)} inputs"
                )
            ledger.record(response.cost, target_lang=target)
            for text in response.texts:
                out[flat_ids[cursor]] = text
                cursor += 1
        return out

    async def _finalize(
        self,
        prepared: list[_Prepared],
        translated_by_id: dict[str, str],
        *,
        source: str,
        target: str,
        ledger: CostLedger,
        back_translate: bool,
    ) -> dict[str, TranslatedSegment]:
        # Optional back-translation of the (masked) outputs in one extra batch.
        # We back-translate the masked target text (sentinels survive both ways)
        # and restore the original markup tokens leniently so the recovered text
        # reads naturally for the similarity comparison against the source.
        back_by_id: dict[str, str] = {}
        token_map = {p.segment.id: p.markup_tokens for p in prepared}
        if back_translate and prepared:
            bt_ids = [p.segment.id for p in prepared if p.segment.id in translated_by_id]
            bt_requests = [
                ProviderRequest(
                    masked_text=translated_by_id[seg_id],
                    source_lang=target,
                    target_lang=source,
                )
                for seg_id in bt_ids
            ]
            cursor = 0
            for batch in batch_requests(bt_requests):
                try:
                    resp = await self._provider.back_translate(batch)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("translation.back_translate_failed", error=str(exc))
                    break
                ledger.record(resp.cost, target_lang=source)
                for text in resp.texts:
                    seg_id = bt_ids[cursor]
                    back_by_id[seg_id] = restore(text, token_map[seg_id], lenient=True)
                    cursor += 1

        results: dict[str, TranslatedSegment] = {}
        for p in prepared:
            masked_out = translated_by_id.get(p.segment.id, "")
            # Restore glossary sentinels first (they were inside the masked text),
            # then markup sentinels.
            warnings: list[str] = []
            if self._glossary is not None and p.glossary_restorations:
                masked_out = Glossary.restore(masked_out, list(p.glossary_restorations))
            try:
                restored = restore(masked_out, p.markup_tokens)
            except Exception as exc:  # noqa: BLE001 - markup corruption → review
                warnings.append(str(exc))
                restored = restore(masked_out, p.markup_tokens, lenient=True)
            restored = prepare_rtl_segment(restored, target)

            back = back_by_id.get(p.segment.id)
            report = estimate_quality(
                source=p.segment.text,
                translated=restored,
                source_lang=source,
                target_lang=target,
                glossary=self._glossary,
                back_translation=back,
            )
            all_warnings = tuple(warnings) + report.warnings
            needs_review = report.score < self._review_threshold or bool(warnings)
            results[p.segment.id] = TranslatedSegment(
                id=p.segment.id,
                source_text=p.segment.text,
                translated_text=restored,
                source_lang=source,
                target_lang=target,
                origin=TranslationOrigin.PROVIDER,
                quality=report.score,
                needs_review=needs_review,
                warnings=all_warnings,
            )
        return results

    # -- convenience ------------------------------------------------------ #

    async def translate_text(
        self,
        text: str,
        *,
        book_id: str,
        target_lang: str,
        source_lang: str | None = None,
        kind: ContentKind = ContentKind.PAGE_TEXT,
        back_translate: bool = False,
    ) -> TranslatedSegment:
        """Translate a single string (wraps it as a one-segment request)."""
        request = TranslationRequest(
            book_id=book_id,
            target_lang=target_lang,
            segments=(Segment(id="0", text=text, kind=kind),),
            source_lang=source_lang,
            back_translate=back_translate,
        )
        result = await self.translate(request)
        return result.segments[0]


__all__ = ["TranslationService"]
