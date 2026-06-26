"""Tests for app.optim.prompt_compress — pure helpers that cut input tokens.

These are opt-in tools (no auto-application), so they cannot change behavior. Their job is to shrink
what an agent *chooses* to send: collapse whitespace, dedupe re-sent canon blocks, trim a message
window to a token budget, and strip verbose JSON-schema chrome before a structured-output call.
"""

from __future__ import annotations

from app.optim.prompt_compress import (
    collapse_whitespace,
    compact_json_schema,
    compression_ratio,
    dedupe_canon,
    estimate_tokens,
    trim_messages_to_budget,
)


def test_estimate_tokens_empty_is_zero() -> None:
    assert estimate_tokens("") == 0


def test_estimate_tokens_uses_chars_per_token_heuristic() -> None:
    # Documented heuristic: ceil(len/4). 8 chars -> 2 tokens.
    assert estimate_tokens("abcdefgh") == 2
    assert estimate_tokens("abcde") == 2  # ceil(5/4) == 2


def test_estimate_tokens_is_monotonic_in_length() -> None:
    assert estimate_tokens("a" * 100) >= estimate_tokens("a" * 10)


def test_collapse_whitespace_reduces_runs_and_token_estimate() -> None:
    messy = "The   cat\n\n\n  sat   on\tthe    mat"
    out = collapse_whitespace(messy)
    assert out == "The cat sat on the mat"
    assert estimate_tokens(out) < estimate_tokens(messy)


def test_dedupe_canon_drops_exact_duplicates_preserving_order() -> None:
    blocks = ["Alice is brave.", "Bob has a sword.", "Alice is brave."]
    assert dedupe_canon(blocks) == ["Alice is brave.", "Bob has a sword."]


def test_dedupe_canon_normalizes_whitespace_before_comparing() -> None:
    blocks = ["Alice  is brave.", "Alice is brave.", "  Alice is brave.  "]
    # Same fact once whitespace-normalized -> one block kept (the first, verbatim).
    assert dedupe_canon(blocks) == ["Alice  is brave."]


def test_trim_messages_keeps_system_and_most_recent_within_budget() -> None:
    messages = [
        {"role": "system", "content": "S" * 40},  # 10 tokens
        {"role": "user", "content": "U1" * 20},  # 10 tokens
        {"role": "assistant", "content": "A1" * 20},  # 10 tokens
        {"role": "user", "content": "U2" * 20},  # 10 tokens
    ]
    # Budget 25 tokens: must keep system (10) + the most recent user (10) and drop the older two.
    out = trim_messages_to_budget(messages, budget_tokens=25)
    roles = [m["role"] for m in out]
    assert roles[0] == "system"
    assert out[-1]["content"] == "U2" * 20
    assert sum(estimate_tokens(str(m["content"])) for m in out) <= 25


def test_trim_messages_never_drops_system_even_over_budget() -> None:
    messages = [
        {"role": "system", "content": "S" * 80},  # 20 tokens
        {"role": "user", "content": "U" * 80},  # 20 tokens
    ]
    out = trim_messages_to_budget(messages, budget_tokens=5)
    assert [m["role"] for m in out] == ["system"]


def test_trim_messages_returns_original_order() -> None:
    messages = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "u2"},
    ]
    out = trim_messages_to_budget(messages, budget_tokens=1000)
    assert out == messages  # nothing dropped, identical order


def test_compact_json_schema_strips_verbose_chrome_recursively() -> None:
    schema = {
        "type": "object",
        "title": "Shot",
        "description": "A shot",
        "default": {},
        "properties": {
            "mode": {"type": "string", "description": "render mode", "examples": ["t2v"]},
            "beats": {
                "type": "array",
                "items": {"type": "object", "title": "Beat", "$comment": "x"},
            },
        },
        "required": ["mode"],
    }
    out = compact_json_schema(schema)
    assert "description" not in out and "title" not in out and "default" not in out
    assert out["type"] == "object"
    assert out["required"] == ["mode"]
    assert "description" not in out["properties"]["mode"]
    assert "examples" not in out["properties"]["mode"]
    assert out["properties"]["mode"]["type"] == "string"
    assert "$comment" not in out["properties"]["beats"]["items"]
    # Original is untouched (pure function).
    assert "description" in schema


def test_compression_ratio_reports_fraction_saved() -> None:
    assert compression_ratio(100, 75) == 0.25
    assert compression_ratio(0, 0) == 0.0  # no division by zero
    assert compression_ratio(100, 100) == 0.0
