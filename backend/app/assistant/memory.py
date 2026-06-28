"""Conversation memory — multi-turn history for a reader's Q&A session.

A grounded follow-up ("and where did she go after that?") needs the prior turns
in context. This module stores a session's exchange history and serves a
*token-bounded* rolling window back to the prompt builder, so a long conversation
never blows the context budget — the same "recall under a limited window"
discipline §8.4 applies to canon, applied to chat history.

Two backends behind one :class:`ConversationStore` protocol:

* :class:`InMemoryConversationStore` — a pure dict, the default for tests and the
  single-process case; deterministic and network-free.
* :class:`RedisConversationStore` — a JSON list per session key with a TTL, for
  the multi-instance API.

The window policy is pure (:func:`window`) so it's tested independently of any
backend.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Protocol

from app.assistant.types import ConversationTurn
from app.memory.retrieval import estimate_tokens

#: Default rolling-window token budget for replayed history.
DEFAULT_HISTORY_TOKENS = 900
#: Default Redis TTL for a conversation (a reading session is bounded).
DEFAULT_CONVERSATION_TTL_S = 60 * 60 * 6


def make_turn(role: str, content: str) -> ConversationTurn:
    """Build a :class:`ConversationTurn` with a coarse token estimate filled in."""
    return ConversationTurn(role=role, content=content, tokens=estimate_tokens(content))


def window(
    turns: list[ConversationTurn], *, token_budget: int = DEFAULT_HISTORY_TOKENS
) -> list[ConversationTurn]:
    """Return the most-recent turns that fit ``token_budget`` (chronological).

    Walks back from the newest turn accumulating tokens until the budget is hit,
    then returns the kept slice in original (oldest-first) order so the prompt
    reads forward in time. Always keeps at least the most recent turn.
    """
    if not turns:
        return []
    kept: list[ConversationTurn] = []
    spent = 0
    for turn in reversed(turns):
        cost = turn.tokens or estimate_tokens(turn.content)
        if kept and spent + cost > token_budget:
            break
        kept.append(turn)
        spent += cost
    kept.reverse()
    return kept


class ConversationStore(Protocol):
    """Append-and-read seam for a session's conversation history."""

    async def append(self, conversation_id: str, turn: ConversationTurn) -> None: ...

    async def history(self, conversation_id: str) -> list[ConversationTurn]: ...

    async def clear(self, conversation_id: str) -> None: ...


class InMemoryConversationStore:
    """A pure in-process conversation store (default; test-friendly)."""

    def __init__(self, *, max_turns: int = 50) -> None:
        self._turns: dict[str, list[ConversationTurn]] = defaultdict(list)
        self._max_turns = max_turns

    async def append(self, conversation_id: str, turn: ConversationTurn) -> None:
        bucket = self._turns[conversation_id]
        bucket.append(turn)
        if len(bucket) > self._max_turns:
            del bucket[: len(bucket) - self._max_turns]

    async def history(self, conversation_id: str) -> list[ConversationTurn]:
        return list(self._turns.get(conversation_id, []))

    async def clear(self, conversation_id: str) -> None:
        self._turns.pop(conversation_id, None)


class RedisConversationStore:
    """A Redis-backed conversation store (JSON list per key, TTL-bounded).

    Stores the whole history under one key as a JSON list of serialized turns —
    a reading conversation is short enough that read-modify-write is fine and
    keeps the implementation a single round-trip each way. Bounded by ``max_turns``
    on write and a TTL so abandoned conversations expire.
    """

    def __init__(
        self,
        redis: object,
        *,
        ttl_s: int = DEFAULT_CONVERSATION_TTL_S,
        max_turns: int = 50,
        key_prefix: str = "kinora:assistant:conv:",
    ) -> None:
        self._redis = redis
        self._ttl_s = ttl_s
        self._max_turns = max_turns
        self._prefix = key_prefix

    def _key(self, conversation_id: str) -> str:
        return f"{self._prefix}{conversation_id}"

    async def append(self, conversation_id: str, turn: ConversationTurn) -> None:
        key = self._key(conversation_id)
        existing = await self._redis.get_json(key) or []  # type: ignore[attr-defined]
        existing.append(turn.model_dump())
        if len(existing) > self._max_turns:
            existing = existing[-self._max_turns :]
        await self._redis.set_json(key, existing, ttl_s=self._ttl_s)  # type: ignore[attr-defined]

    async def history(self, conversation_id: str) -> list[ConversationTurn]:
        raw = await self._redis.get_json(self._key(conversation_id)) or []  # type: ignore[attr-defined]
        return [ConversationTurn.model_validate(t) for t in raw]

    async def clear(self, conversation_id: str) -> None:
        await self._redis.delete(self._key(conversation_id))  # type: ignore[attr-defined]


class ConversationMemory:
    """High-level memory: append a Q/A pair, serve a token-bounded window."""

    def __init__(
        self,
        store: ConversationStore | None = None,
        *,
        token_budget: int = DEFAULT_HISTORY_TOKENS,
    ) -> None:
        self._store = store or InMemoryConversationStore()
        self._budget = token_budget

    async def recall(self, conversation_id: str) -> list[ConversationTurn]:
        """The recent history window for ``conversation_id`` (token-bounded)."""
        if not conversation_id:
            return []
        turns = await self._store.history(conversation_id)
        return window(turns, token_budget=self._budget)

    async def record(self, conversation_id: str, *, question: str, answer: str) -> None:
        """Append a user question and the assistant's answer to the history."""
        if not conversation_id:
            return
        await self._store.append(conversation_id, make_turn("user", question))
        await self._store.append(conversation_id, make_turn("assistant", answer))

    async def clear(self, conversation_id: str) -> None:
        if conversation_id:
            await self._store.clear(conversation_id)


__all__ = [
    "DEFAULT_CONVERSATION_TTL_S",
    "DEFAULT_HISTORY_TOKENS",
    "ConversationMemory",
    "ConversationStore",
    "InMemoryConversationStore",
    "RedisConversationStore",
    "make_turn",
    "window",
]
