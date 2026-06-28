"""Shared test doubles for the reader-assistant suite (zero network, zero credits).

Every seam the assistant touches — the chat provider, the embedder, and the
canon read model over the DB — has a deterministic fake here so the whole turn
runs offline. Imported by ``test_assistant_*.py``.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from typing import Any

from app.assistant.types import ReadingPosition, RetrievedSpan, SourceKind
from app.providers.types import ChatResult

_EMBED_DIM = 1152


class FakeEmbedder:
    """Deterministic bag-of-words embedder so cosine tracks lexical overlap.

    Each token hashes to an axis; a text's vector is the multiset of its token
    axes. Two texts that share words get a positive cosine, so dense scoring is
    meaningful in tests without a live model. Unit-free (the retriever normalizes
    via cosine), shared image+text space shape (1152-d) like the real provider.
    """

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        return [self._vec(t) for t in texts]

    async def embed_images(self, images: list[bytes]) -> list[list[float]]:
        return [self._vec(b.decode("utf-8", "ignore")) for b in images]

    @staticmethod
    def _vec(text: str) -> list[float]:
        import re

        vec = [0.0] * _EMBED_DIM
        for tok in re.findall(r"[a-z0-9]+", text.lower()):
            axis = int.from_bytes(hashlib.sha1(tok.encode()).digest()[:4], "big") % _EMBED_DIM
            vec[axis] += 1.0
        return vec


class FakeChat:
    """A scriptable chat client satisfying ``synth.ChatClient``.

    ``answer_for(question) -> (prose, citations)`` is the hook tests override; by
    default it echoes the first context line's marker so the grounding guard sees
    a valid citation. ``chat_json`` wraps that into the JSON answer contract;
    ``chat`` returns the prose as a :class:`ChatResult` for the streaming path.
    """

    def __init__(
        self,
        *,
        answer: str | None = None,
        citations: list[int] | None = None,
        refused: bool = False,
        raise_error: bool = False,
    ) -> None:
        self.answer = answer
        self.citations = citations
        self.refused = refused
        self.raise_error = raise_error
        self.calls: list[dict[str, Any]] = []

    def _resolve(self, messages: list[dict[str, Any]]) -> tuple[str, list[int]]:
        if self.answer is not None:
            return self.answer, (self.citations or [])
        # Default: cite [1] and quote a few words so faithfulness can pass.
        return ("The character is described here [1].", [1])

    async def chat_json(
        self,
        messages: list[dict[str, Any]],
        model: str,
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        stream: bool | None = None,
    ) -> dict[str, Any] | list[Any]:
        self.calls.append({"messages": messages, "model": model, "json": True})
        if self.raise_error:
            raise RuntimeError("boom")
        if self.refused:
            return {"answer": "", "citations": [], "refused": True}
        prose, cites = self._resolve(messages)
        return {"answer": prose, "citations": cites, "refused": False}

    async def chat(
        self,
        messages: list[dict[str, Any]],
        model: str,
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        stream: bool | None = None,
    ) -> ChatResult:
        self.calls.append({"messages": messages, "model": model, "json": False})
        if self.raise_error:
            raise RuntimeError("boom")
        prose, _ = self._resolve(messages)
        return ChatResult(text=prose, model=model, input_tokens=10, output_tokens=5)


class FakeReadModel:
    """An in-memory ``CanonReadModel`` over a fixed list of spans.

    ``resolve_ceiling_beat`` honors an explicit beat or ``allow_full_book`` and
    otherwise returns the max beat ordinal whose span starts at/before the
    position's word index — enough to exercise the spoiler gate deterministically.
    """

    def __init__(self, spans: Sequence[RetrievedSpan]) -> None:
        self._spans = list(spans)

    async def resolve_ceiling_beat(self, position: ReadingPosition) -> int:
        if position.allow_full_book:
            return 1 << 62
        if position.beat_index is not None:
            return position.beat_index
        if position.word_index is not None:
            word = position.word_index
            below = [
                s.ordinal
                for s in self._spans
                if (ws := (s.meta or {}).get("word_start")) is not None and ws <= word
            ]
            return max(below) if below else 0
        return 0

    async def candidate_spans(
        self,
        book_id: str,
        *,
        kinds: Sequence[SourceKind] | None = None,
        limit: int = 200,
    ) -> list[RetrievedSpan]:
        wanted = set(kinds) if kinds else None
        out = [
            s.model_copy(deep=True)
            for s in self._spans
            if wanted is None or s.kind in wanted
        ]
        return out[:limit] if limit else out


def make_spans() -> list[RetrievedSpan]:
    """A small, ordered book slice spanning all source kinds + a future spoiler."""
    return [
        RetrievedSpan(
            span_id="canon:char_elsa",
            kind=SourceKind.CANON,
            text="Elsa is a young woman with a platinum braid and an ice-blue gown.",
            ordinal=1,
            locator="Elsa (character)",
            meta={"entity_key": "char_elsa", "name": "Elsa", "entity_type": "character"},
        ),
        RetrievedSpan(
            span_id="canon:loc_castle",
            kind=SourceKind.CANON,
            text="The ice castle stands on the north mountain, glittering and cold.",
            ordinal=2,
            locator="Ice Castle (location)",
            meta={"entity_key": "loc_castle", "name": "Ice Castle", "entity_type": "location"},
        ),
        RetrievedSpan(
            span_id="page:3:0",
            kind=SourceKind.PAGE,
            text="Elsa climbed the north mountain alone, the wind tearing at her gown.",
            ordinal=2,
            locator="p.3",
            meta={"page": 3, "word_start": 0},
        ),
        RetrievedSpan(
            span_id="beat:2",
            kind=SourceKind.BEAT,
            text="Elsa flees to the mountain and builds an ice castle.",
            ordinal=2,
            locator="beat 2",
            meta={"beat_index": 2},
        ),
        RetrievedSpan(
            span_id="shot:s1",
            kind=SourceKind.SHOT,
            text="A wide shot of Elsa raising her hands as the castle erupts from ice.",
            ordinal=2,
            locator="film @ beat_2",
            meta={"shot_id": "s1", "beat_id": "beat_2"},
        ),
        # FUTURE spoiler — beat 9, must be dropped for a reader at beat <= 8.
        RetrievedSpan(
            span_id="canon:char_villain",
            kind=SourceKind.CANON,
            text="The Duke of Weselton sends assassins to the castle later in the story.",
            ordinal=9,
            locator="Duke (character)",
            meta={"entity_key": "char_villain", "name": "Duke", "entity_type": "character"},
        ),
    ]
