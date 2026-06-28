"""The assistant service — orchestrate one grounded Q&A turn end-to-end.

This is the package's public entry point. It wires the layers into the §8 read
flow for a reader's question:

```
classify intent (intents)
  → retrieve spoiler-safe spans (retrieval, over the read model + embedder)
  → assemble a numbered, budget-bounded context (context)
  → recall the conversation window (memory)
  → synthesize a grounded answer over the chat seam (synth + prompts)
  → guard citations against the context (grounding, inside synth)
  → record the turn + build follow-ups (memory + suggest)
```

It exposes a non-streaming :meth:`ask` (returns a complete :class:`AssistantTurn`)
and a streaming :meth:`ask_stream` (yields :class:`StreamDelta` s for the live UI,
then records + suggests once the answer is final). Both reuse the same retrieval +
assembly so the grounded answer is identical whether streamed or not.

Everything heavy is injected: the :class:`CanonReadModel` (DB), the
:class:`Embedder`, the chat client. In tests they're fakes — the service runs a
full turn with zero network and zero credits.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from app.assistant.context import ContextAssembler
from app.assistant.intents import classify_intent
from app.assistant.memory import ConversationMemory
from app.assistant.read_model import CanonReadModel
from app.assistant.retrieval import RetrievalConfig, Retriever
from app.assistant.suggest import DEFAULT_SUGGESTION_COUNT, suggest_questions
from app.assistant.synth import AnswerSynthesizer
from app.assistant.types import (
    AssistantTurn,
    ReadingPosition,
    StreamDelta,
)
from app.core.logging import get_logger
from app.memory.interfaces import Embedder

logger = get_logger("app.assistant.service")


@dataclass
class AssistantConfig:
    """Per-service tunables (retrieval + assembly + suggestions)."""

    retrieval: RetrievalConfig = field(default_factory=RetrievalConfig)
    context_tokens: int = 1800
    suggestion_count: int = DEFAULT_SUGGESTION_COUNT
    history_tokens: int = 900


class AssistantService:
    """Grounded, spoiler-aware Q&A over one book's canon + pages."""

    def __init__(
        self,
        read_model: CanonReadModel,
        synthesizer: AnswerSynthesizer,
        *,
        embedder: Embedder | None = None,
        memory: ConversationMemory | None = None,
        config: AssistantConfig | None = None,
    ) -> None:
        self._config = config or AssistantConfig()
        self._retriever = Retriever(read_model, embedder=embedder)
        self._assembler = ContextAssembler(token_budget=self._config.context_tokens)
        self._synth = synthesizer
        self._memory = memory or ConversationMemory(token_budget=self._config.history_tokens)

    async def ask(
        self,
        book_id: str,
        question: str,
        position: ReadingPosition,
        *,
        conversation_id: str = "",
    ) -> AssistantTurn:
        """Answer ``question`` at ``position``; return the full grounded turn."""
        intent = classify_intent(question).intent
        retrieval = await self._retriever.retrieve(
            book_id, question, position, intent=intent, config=self._config.retrieval
        )
        context = self._assembler.assemble(retrieval.spans)
        history = await self._memory.recall(conversation_id)

        answer = await self._synth.synthesize(
            question, context, intent=intent, history=history
        )

        await self._memory.record(conversation_id, question=question, answer=answer.text)
        suggestions = suggest_questions(
            retrieval.spans, asked=question, limit=self._config.suggestion_count
        )
        logger.info(
            "assistant.answered",
            book_id=book_id,
            intent=intent.value,
            spans=len(context.marker_to_span),
            dropped=retrieval.spoiler.drop_count,
            coverage=answer.citation_coverage,
            refused=answer.refused,
        )
        return AssistantTurn(
            question=question,
            intent=intent,
            answer=answer,
            suggestions=suggestions,
            context_span_ids=context.span_ids,
        )

    async def suggestions_for(
        self,
        book_id: str,
        position: ReadingPosition,
        *,
        limit: int = DEFAULT_SUGGESTION_COUNT,
    ) -> list:
        """Spoiler-safe suggested questions for a position (no question asked).

        Retrieves a generic recap-shaped slice (so the suggestions reflect the
        reader's current reach) and mines follow-ups from it. Returns
        :class:`~app.assistant.types.SuggestedQuestion` s.
        """
        result = await self._retriever.retrieve(
            book_id,
            "what has happened so far",
            position,
            config=self._config.retrieval,
        )
        return suggest_questions(result.spans, limit=limit)

    async def ask_stream(
        self,
        book_id: str,
        question: str,
        position: ReadingPosition,
        *,
        conversation_id: str = "",
    ) -> AsyncIterator[StreamDelta]:
        """Stream the answer; emit a final ``done`` delta, then record + suggest."""
        intent = classify_intent(question).intent
        retrieval = await self._retriever.retrieve(
            book_id, question, position, intent=intent, config=self._config.retrieval
        )
        context = self._assembler.assemble(retrieval.spans)
        history = await self._memory.recall(conversation_id)

        final_answer = None
        async for delta in self._synth.stream(
            question, context, intent=intent, history=history
        ):
            if delta.type == "done" and delta.answer is not None:
                final_answer = delta.answer
                # Enrich the terminal delta with suggestions before yielding.
                suggestions = suggest_questions(
                    retrieval.spans, asked=question, limit=self._config.suggestion_count
                )
                yield StreamDelta(
                    type="done", answer=final_answer, suggestions=suggestions
                )
            else:
                yield delta

        if final_answer is not None:
            await self._memory.record(
                conversation_id, question=question, answer=final_answer.text
            )


__all__ = ["AssistantConfig", "AssistantService"]
