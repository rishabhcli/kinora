"""Tests for conversation memory (window policy + stores)."""

from __future__ import annotations

from app.assistant.memory import (
    ConversationMemory,
    InMemoryConversationStore,
    make_turn,
    window,
)
from app.assistant.types import ConversationTurn


def test_window_keeps_recent_within_budget() -> None:
    turns = [ConversationTurn(role="user", content="x", tokens=100) for _ in range(10)]
    kept = window(turns, token_budget=250)
    # 250 budget / 100 per turn → at most ~2 turns, the most recent.
    assert len(kept) <= 3
    assert kept == turns[-len(kept) :]


def test_window_always_keeps_at_least_one() -> None:
    turns = [ConversationTurn(role="user", content="x", tokens=10_000)]
    assert window(turns, token_budget=1) == turns


def test_window_empty() -> None:
    assert window([]) == []


def test_make_turn_estimates_tokens() -> None:
    turn = make_turn("user", "hello there world")
    assert turn.role == "user"
    assert turn.tokens >= 1


async def test_in_memory_store_round_trip() -> None:
    store = InMemoryConversationStore()
    await store.append("c1", make_turn("user", "Who is Elsa?"))
    await store.append("c1", make_turn("assistant", "She has a braid."))
    hist = await store.history("c1")
    assert [t.role for t in hist] == ["user", "assistant"]
    await store.clear("c1")
    assert await store.history("c1") == []


async def test_in_memory_store_caps_turns() -> None:
    store = InMemoryConversationStore(max_turns=4)
    for i in range(10):
        await store.append("c", make_turn("user", f"q{i}"))
    hist = await store.history("c")
    assert len(hist) == 4
    assert hist[-1].content == "q9"


async def test_memory_record_and_recall() -> None:
    mem = ConversationMemory(token_budget=10_000)
    await mem.record("conv", question="Who is Elsa?", answer="She has a braid.")
    hist = await mem.recall("conv")
    assert len(hist) == 2
    assert hist[0].content == "Who is Elsa?"
    assert hist[1].content == "She has a braid."


async def test_memory_no_conversation_id_is_noop() -> None:
    mem = ConversationMemory()
    await mem.record("", question="q", answer="a")
    assert await mem.recall("") == []
