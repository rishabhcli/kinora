"""OCR fallback for scanned / image-only pages (§9.1 step 1).

PyMuPDF's text extraction returns nothing for a page that is a **scanned image**
(a photographed/flattened page with no embedded text layer) — common in old
public-domain scans, manga, and image-first PDFs. Such a page would contribute
**zero words** to the book-global word index, so the §4.2 source-span index would
have a gap there and the reader would scroll through "blank" pages with no shots.

This module is the fix. It provides:

* :func:`looks_scanned` — a cheap heuristic that flags a page whose extractable
  word count is far below what its rendered ink-coverage predicts (i.e. there is
  clearly visible text in the image that the text layer missed);
* an :class:`OcrEngine` protocol so the transcription backend is a swappable
  seam — the default :class:`VlOcrEngine` asks Qwen-VL to transcribe the page
  image (no extra dependency, reuses the provider already wired for §9.1 step 2),
  and a local Tesseract engine can drop in for offline / cost-sensitive deploys;
* :func:`synthesize_word_boxes` — lays the OCR'd words out in a simple top-to-
  bottom grid of normalised ``[x, y, w, h]`` boxes so the karaoke highlight layer
  (§9.4) still has *some* geometry to paint, even though the true per-word
  positions were lost with the missing text layer.

OCR is **off by default** (``settings.ingest_ocr_enabled``) and gated so it only
fires on pages the heuristic flags — a born-digital book never pays the token
cost. The unit tests use a fake engine; no network is required.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol

from app.core.logging import get_logger

logger = get_logger("app.ingest.ocr")

#: A page with at least this many extracted words is assumed to have a good text
#: layer and is never OCR'd (saves tokens on born-digital books).
_GOOD_TEXT_WORD_FLOOR = 12
#: When the text layer has fewer words than the floor *and* the page image is
#: large enough to plausibly hold a full page of text, it is treated as scanned.
_MIN_SCANNED_IMAGE_BYTES = 8 * 1024
#: Words per synthetic line + lines per synthetic page block (layout grid).
_WORDS_PER_LINE = 10
#: Vertical text margins for the synthetic grid (normalised page coords).
_TOP_MARGIN = 0.06
_BOTTOM_MARGIN = 0.06
_LEFT_MARGIN = 0.07
_RIGHT_MARGIN = 0.07

_OCR_PROMPT = (
    "You are an OCR transcription engine. The image is a single page of a book. "
    "Transcribe ALL readable text on the page, in natural reading order, exactly "
    "as written. Do not summarise, translate, describe, or add commentary — "
    "output ONLY the transcribed text. If the page has no readable text, output "
    "an empty response."
)

_WORD_SPLIT = re.compile(r"\S+")


class OcrError(RuntimeError):
    """Raised when an OCR engine fails irrecoverably for a page."""


@dataclass(frozen=True, slots=True)
class OcrWord:
    """One OCR'd word and its synthesised normalised ``[x, y, w, h]`` box."""

    text: str
    bbox: tuple[float, float, float, float]


@dataclass(frozen=True, slots=True)
class OcrResult:
    """The transcription of one page: the raw text + synthesised word boxes."""

    text: str
    words: list[OcrWord]

    @property
    def num_words(self) -> int:
        return len(self.words)


class OcrEngine(Protocol):
    """A page-image → transcribed-text backend (the swappable OCR seam).

    Implementations must be async and must return the page's text (empty string
    for a blank page); they must not raise for an ordinary blank page.
    """

    async def transcribe(self, image: bytes, *, page_number: int) -> str:
        """Transcribe the readable text of one page image."""
        ...


def looks_scanned(*, num_text_words: int, image_size_bytes: int) -> bool:
    """Heuristic: does this page look like a scanned/image-only page?

    True when the embedded text layer yielded almost nothing (``< floor`` words)
    yet the rendered page image is substantial enough to plausibly contain a full
    page of text. This deliberately errs toward *not* OCR-ing (the floor is low),
    because OCR costs tokens and a genuinely sparse page (a chapter title, a
    blank verso) should not trigger it spuriously — but a 0-word, image-heavy
    page is the classic scan signature.
    """
    if num_text_words >= _GOOD_TEXT_WORD_FLOOR:
        return False
    return image_size_bytes >= _MIN_SCANNED_IMAGE_BYTES


def synthesize_word_boxes(text: str) -> list[OcrWord]:
    """Lay OCR'd ``text`` out in a top-to-bottom reading grid of normalised boxes.

    The true per-word geometry was lost with the page's missing text layer, so we
    distribute the words across a simple grid within the page's text margins. The
    karaoke layer then highlights *roughly* the right region as narration plays —
    far better than no highlight at all, and the word *order* (what drives the
    page-turn + scroll sync) is exact.
    """
    tokens = _WORD_SPLIT.findall(text)
    if not tokens:
        return []

    num_lines = max(1, (len(tokens) + _WORDS_PER_LINE - 1) // _WORDS_PER_LINE)
    usable_h = max(1e-6, 1.0 - _TOP_MARGIN - _BOTTOM_MARGIN)
    usable_w = max(1e-6, 1.0 - _LEFT_MARGIN - _RIGHT_MARGIN)
    line_h = usable_h / num_lines
    box_h = min(line_h * 0.7, line_h)

    words: list[OcrWord] = []
    for i, token in enumerate(tokens):
        line = i // _WORDS_PER_LINE
        col = i % _WORDS_PER_LINE
        cell_w = usable_w / _WORDS_PER_LINE
        x = _LEFT_MARGIN + col * cell_w
        y = _TOP_MARGIN + line * line_h
        # A word box spans ~90% of its cell, vertically centred in the line.
        w = cell_w * 0.9
        words.append(
            OcrWord(
                text=token,
                bbox=(
                    round(x, 5),
                    round(y + (line_h - box_h) / 2.0, 5),
                    round(w, 5),
                    round(box_h, 5),
                ),
            )
        )
    return words


async def ocr_page(engine: OcrEngine, image: bytes, *, page_number: int) -> OcrResult:
    """Transcribe one page image and synthesise its word boxes.

    Never raises for an ordinary failure: a transcription error or a blank page
    yields an empty :class:`OcrResult` (logged), so a single bad page never
    fails the whole ingest — exactly like the analyse pass (§9.1 step 2).
    """
    try:
        text = (await engine.transcribe(image, page_number=page_number) or "").strip()
    except Exception as exc:  # noqa: BLE001 - one OCR failure must not kill ingest
        logger.warning("ingest.ocr.page_failed", page_number=page_number, error=str(exc))
        return OcrResult(text="", words=[])
    words = synthesize_word_boxes(text)
    if words:
        logger.info("ingest.ocr.page_done", page_number=page_number, words=len(words))
    return OcrResult(text=text, words=words)


class VlOcrEngine:
    """Default OCR engine — transcribes the page image with Qwen-VL (§9.1).

    Reuses the VL provider already wired for the analyse pass, so OCR needs no new
    model or dependency: it sends the page PNG with a strict transcription prompt
    and returns the model's text. The VL model is a strong general OCR for printed
    and even handwritten pages.
    """

    def __init__(
        self,
        vl: object,
        *,
        model: str | None = None,
        max_tokens: int = 2048,
    ) -> None:
        self._vl = vl
        self._model = model
        self._max_tokens = max_tokens

    async def transcribe(self, image: bytes, *, page_number: int) -> str:
        analyze = getattr(self._vl, "analyze", None)
        if analyze is None:  # pragma: no cover - defensive, real VL always has it
            raise OcrError("VL provider has no analyze method")
        result = await analyze(
            [image], _OCR_PROMPT, model=self._model, max_tokens=self._max_tokens
        )
        return str(result or "")


__all__ = [
    "OcrEngine",
    "OcrError",
    "OcrResult",
    "OcrWord",
    "VlOcrEngine",
    "looks_scanned",
    "ocr_page",
    "synthesize_word_boxes",
]
