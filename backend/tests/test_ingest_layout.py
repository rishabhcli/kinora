"""Layout / column / reading-order unit tests (§9.1 step 1) — pure, no infra.

Builds synthetic word geometries and asserts the reading-order rethread reads a
two-column page column-by-column (not zig-zag) while leaving a single-column
page untouched.
"""

from __future__ import annotations

import fitz  # PyMuPDF

from app.ingest.layout import (
    Word,
    detect_columns,
    order_raw_words,
    reading_order,
    to_words,
)

PAGE_W = 600.0
PAGE_H = 800.0


def _word(text: str, x0: float, y0: float, *, w: float = 40.0, h: float = 12.0) -> Word:
    return Word(x0=x0, y0=y0, x1=x0 + w, y1=y0 + h, text=text, block=0, line=0, word=0)


# --------------------------------------------------------------------------- #
# Column detection
# --------------------------------------------------------------------------- #


def test_single_column_detected_as_one() -> None:
    words = [_word(f"w{i}", 50.0, 40.0 + i * 20) for i in range(5)]
    cols = detect_columns(words, PAGE_W)
    assert len(cols) == 1


def test_two_columns_detected() -> None:
    left = [_word(f"L{i}", 40.0, 40.0 + i * 20) for i in range(5)]
    right = [_word(f"R{i}", 340.0, 40.0 + i * 20) for i in range(5)]
    cols = detect_columns(left + right, PAGE_W)
    assert len(cols) == 2
    assert cols[0].x_lo == 0.0
    assert cols[1].x_hi == PAGE_W


def test_narrow_gap_is_not_a_column() -> None:
    # Two word stacks 10pt apart — ordinary spacing, not a gutter.
    a = [_word(f"a{i}", 40.0, 40.0 + i * 20) for i in range(4)]
    b = [_word(f"b{i}", 95.0, 40.0 + i * 20) for i in range(4)]
    cols = detect_columns(a + b, PAGE_W)
    assert len(cols) == 1


def test_narrow_page_never_multi_column() -> None:
    words = [_word(f"w{i}", 10.0, 10.0 + i * 20) for i in range(3)]
    cols = detect_columns(words, 120.0)
    assert len(cols) == 1


# --------------------------------------------------------------------------- #
# Reading order
# --------------------------------------------------------------------------- #


def test_two_column_reading_order_is_column_by_column() -> None:
    # Interleave the input the way sort=True would (by row across the gutter).
    words: list[Word] = []
    for row in range(4):
        y = 40.0 + row * 20
        words.append(_word(f"L{row}", 40.0, y))
        words.append(_word(f"R{row}", 340.0, y))
    result = reading_order(words, PAGE_W, PAGE_H)
    assert result.is_multi_column
    texts = [w.text for w in result.words]
    # Whole left column, then whole right column.
    assert texts == ["L0", "L1", "L2", "L3", "R0", "R1", "R2", "R3"]


def test_single_column_reading_order_top_to_bottom() -> None:
    words = [_word(f"w{i}", 50.0, 40.0 + i * 20) for i in range(5)]
    # Shuffle input order; reading order should restore top-to-bottom.
    shuffled = [words[3], words[0], words[4], words[1], words[2]]
    result = reading_order(shuffled, PAGE_W, PAGE_H)
    assert not result.is_multi_column
    assert [w.text for w in result.words] == ["w0", "w1", "w2", "w3", "w4"]


def test_same_line_keeps_left_to_right() -> None:
    a = _word("alpha", 50.0, 40.0)
    b = _word("beta", 120.0, 41.0)  # tiny baseline jitter, same visual line
    c = _word("gamma", 200.0, 40.5)
    result = reading_order([c, a, b], PAGE_W, PAGE_H)
    assert [w.text for w in result.words] == ["alpha", "beta", "gamma"]


def test_empty_page() -> None:
    result = reading_order([], PAGE_W, PAGE_H)
    assert result.words == []
    assert result.num_columns == 1


# --------------------------------------------------------------------------- #
# Integration with a real PyMuPDF two-column page
# --------------------------------------------------------------------------- #


def test_order_raw_words_on_real_two_column_pdf() -> None:
    doc = fitz.open()
    page = doc.new_page(width=600, height=800)
    page.insert_textbox(
        fitz.Rect(40, 40, 270, 760),
        "Leftcol one two three four five six seven eight nine ten.",
        fontsize=11,
    )
    page.insert_textbox(
        fitz.Rect(330, 40, 560, 760),
        "Rightcol alpha beta gamma delta epsilon zeta eta theta iota.",
        fontsize=11,
    )
    raw = page.get_text("words", sort=True)
    ordered = order_raw_words(raw, page.rect.width, page.rect.height)
    texts = [w.text for w in ordered]
    left_words = {"Leftcol", "one", "two", "three", "four", "five", "six",
                  "seven", "eight", "nine", "ten."}
    right_words = {"Rightcol", "alpha", "beta", "gamma", "delta", "epsilon",
                   "zeta", "eta", "theta", "iota."}
    left_indices = [i for i, t in enumerate(texts) if t in left_words]
    right_indices = [i for i, t in enumerate(texts) if t in right_words]
    # Every left-column word precedes every right-column word in reading order.
    assert max(left_indices) < min(right_indices)
    doc.close()


def test_to_words_skips_blank() -> None:
    raw = [
        (0.0, 0.0, 10.0, 10.0, "hello", 0, 0, 0),
        (0.0, 0.0, 10.0, 10.0, "   ", 0, 0, 1),
        (0.0, 0.0, 10.0, 10.0, "world", 0, 0, 2),
    ]
    words = to_words(raw)
    assert [w.text for w in words] == ["hello", "world"]
