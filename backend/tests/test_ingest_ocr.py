"""OCR-fallback unit tests (§9.1 step 1) — pure, fake engine, no network.

Exercises the scanned-page heuristic, synthetic word-box layout, the
fail-soft :func:`ocr_page` path, and the VL-backed default engine against a
stub VL provider.
"""

from __future__ import annotations

import pytest

from app.ingest.ocr import (
    OcrResult,
    VlOcrEngine,
    looks_scanned,
    ocr_page,
    synthesize_word_boxes,
)

# --------------------------------------------------------------------------- #
# Heuristic
# --------------------------------------------------------------------------- #


def test_good_text_layer_is_not_scanned() -> None:
    assert looks_scanned(num_text_words=400, image_size_bytes=200_000) is False


def test_zero_words_large_image_is_scanned() -> None:
    assert looks_scanned(num_text_words=0, image_size_bytes=200_000) is True


def test_zero_words_tiny_image_not_scanned() -> None:
    # A tiny image is probably a decorative glyph / blank verso, not a scan.
    assert looks_scanned(num_text_words=0, image_size_bytes=1024) is False


def test_a_few_words_still_scanned_if_image_big() -> None:
    assert looks_scanned(num_text_words=3, image_size_bytes=100_000) is True


# --------------------------------------------------------------------------- #
# Synthetic word boxes
# --------------------------------------------------------------------------- #


def test_synthesize_word_boxes_order_and_bounds() -> None:
    text = " ".join(f"word{i}" for i in range(25))
    words = synthesize_word_boxes(text)
    assert [w.text for w in words] == [f"word{i}" for i in range(25)]
    for w in words:
        x, y, bw, bh = w.bbox
        assert 0.0 <= x <= 1.0 and 0.0 <= y <= 1.0
        assert 0.0 <= bw <= 1.0 and 0.0 <= bh <= 1.0
        assert x + bw <= 1.0 + 1e-6
        assert y + bh <= 1.0 + 1e-6


def test_synthesize_word_boxes_rows_advance_downward() -> None:
    text = " ".join(f"w{i}" for i in range(30))  # 3 lines of 10
    words = synthesize_word_boxes(text)
    # The first word of line 2 sits below the first word of line 1.
    assert words[10].bbox[1] > words[0].bbox[1]
    assert words[20].bbox[1] > words[10].bbox[1]


def test_synthesize_empty() -> None:
    assert synthesize_word_boxes("   ") == []


# --------------------------------------------------------------------------- #
# ocr_page — fail-soft
# --------------------------------------------------------------------------- #


class _FakeEngine:
    def __init__(self, text: str = "", *, raises: bool = False) -> None:
        self.text = text
        self.raises = raises
        self.calls: list[int] = []

    async def transcribe(self, image: bytes, *, page_number: int) -> str:
        self.calls.append(page_number)
        if self.raises:
            raise RuntimeError("boom")
        return self.text


async def test_ocr_page_transcribes_and_boxes() -> None:
    engine = _FakeEngine("The cat sat on the mat by the warm fire all night.")
    result = await ocr_page(engine, b"\x89PNGfake", page_number=7)
    assert isinstance(result, OcrResult)
    assert result.num_words == 12
    assert result.text.startswith("The cat")
    assert engine.calls == [7]


async def test_ocr_page_blank_is_empty() -> None:
    result = await ocr_page(_FakeEngine(""), b"img", page_number=1)
    assert result.num_words == 0
    assert result.text == ""


async def test_ocr_page_failure_is_soft() -> None:
    result = await ocr_page(_FakeEngine(raises=True), b"img", page_number=2)
    assert result.num_words == 0
    assert result.text == ""


# --------------------------------------------------------------------------- #
# VL-backed default engine
# --------------------------------------------------------------------------- #


class _StubVL:
    def __init__(self, text: str) -> None:
        self.text = text
        self.prompts: list[str] = []

    async def analyze(self, images: list[bytes], prompt: str, **kwargs: object) -> str:
        self.prompts.append(prompt)
        return self.text


async def test_vl_ocr_engine_transcribes() -> None:
    vl = _StubVL("Transcribed page text here.")
    engine = VlOcrEngine(vl, max_tokens=512)
    out = await engine.transcribe(b"pngbytes", page_number=3)
    assert out == "Transcribed page text here."
    assert "OCR" in vl.prompts[0] or "Transcribe" in vl.prompts[0]


async def test_vl_ocr_engine_handles_none() -> None:
    class _NoneVL:
        async def analyze(self, images: list[bytes], prompt: str, **kwargs: object) -> None:
            return None

    engine = VlOcrEngine(_NoneVL())
    assert await engine.transcribe(b"x", page_number=1) == ""


async def test_vl_ocr_engine_missing_analyze_raises() -> None:
    from app.ingest.ocr import OcrError

    engine = VlOcrEngine(object())
    with pytest.raises(OcrError):
        await engine.transcribe(b"x", page_number=1)
