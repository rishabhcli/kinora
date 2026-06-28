"""Unit tests for markup/placeholder masking (mask → translate → restore)."""

from __future__ import annotations

import pytest

from app.translation.errors import MarkupError
from app.translation.markup import (
    mask,
    placeholder_signature,
    restore,
    verify_roundtrip,
)


def test_mask_protects_html_braces_printf_urls() -> None:
    text = "Hello <b>{name}</b>, see https://x.io or mail a@b.co at %s now"
    masked = mask(text)
    # Every protected run replaced by a sentinel; tokens recoverable in order.
    assert "<b>" in masked.tokens
    assert "{name}" in masked.tokens
    assert "</b>" in masked.tokens
    assert "https://x.io" in masked.tokens
    assert "a@b.co" in masked.tokens
    assert "%s" in masked.tokens
    # The masked text holds no raw markup.
    assert "<b>" not in masked.text
    assert "{name}" not in masked.text


def test_roundtrip_restores_exactly() -> None:
    text = "A <i>{x}</i> and %(count)d items at https://k.io/path?q=1"
    masked = mask(text)
    assert restore(masked.text, masked.tokens) == text


def test_restore_detects_dropped_placeholder() -> None:
    masked = mask("keep {a} and {b}")
    # Simulate a translation that dropped the second sentinel.
    broken = masked.text.replace("⟦1⟧", "")
    with pytest.raises(MarkupError):
        restore(broken, masked.tokens)


def test_restore_detects_duplicated_placeholder() -> None:
    masked = mask("only {a} here")
    duplicated = masked.text + "⟦0⟧"
    with pytest.raises(MarkupError):
        restore(duplicated, masked.tokens)


def test_restore_out_of_range_raises() -> None:
    with pytest.raises(MarkupError):
        restore("⟦5⟧", ("only-one",))


def test_lenient_restore_appends_dropped() -> None:
    masked = mask("a {x} b")
    broken = masked.text.replace("⟦0⟧", "")
    out = restore(broken, masked.tokens, lenient=True)
    assert "{x}" in out  # appended at end rather than raising


def test_no_markup_is_noop() -> None:
    masked = mask("plain sentence with no markup")
    assert masked.tokens == ()
    assert masked.text == "plain sentence with no markup"


def test_placeholder_signature_order_independent() -> None:
    assert placeholder_signature("{a} {b}") == placeholder_signature("{b} {a}")
    assert placeholder_signature("{a}") != placeholder_signature("{a} {b}")


def test_verify_roundtrip_flags_dropped_and_introduced() -> None:
    assert verify_roundtrip("keep {a}", "garde {a}") == []
    dropped = verify_roundtrip("keep {a} and {b}", "garde {a}")
    assert any("dropped" in w for w in dropped)
    introduced = verify_roundtrip("keep {a}", "garde {a} {z}")
    assert any("introduced" in w or "duplicated" in w for w in introduced)


def test_self_closing_and_attributed_tags() -> None:
    text = 'line<br/> and <span class="hl">x</span>'
    masked = mask(text)
    assert "<br/>" in masked.tokens
    assert '<span class="hl">' in masked.tokens
    assert restore(masked.text, masked.tokens) == text
