"""End-to-end service tests — full turn with every seam faked (no network)."""

from __future__ import annotations

from app.assistant.memory import ConversationMemory
from app.assistant.service import AssistantService
from app.assistant.synth import AnswerSynthesizer
from app.assistant.types import AssistantIntent, ReadingPosition, StreamDelta
from tests.assistant_fakes import FakeChat, FakeEmbedder, FakeReadModel, make_spans


def _service(
    chat: FakeChat | None = None, *, memory: ConversationMemory | None = None
) -> AssistantService:
    rm = FakeReadModel(make_spans())
    chat = chat or FakeChat(answer="Elsa has a platinum braid [1].", citations=[1])
    synth = AnswerSynthesizer(chat, "model-x")
    return AssistantService(rm, synth, embedder=FakeEmbedder(), memory=memory)


async def test_ask_returns_grounded_turn() -> None:
    svc = _service()
    pos = ReadingPosition(book_id="b", beat_index=8)
    turn = await svc.ask("b", "Who is Elsa?", pos)
    assert turn.intent is AssistantIntent.WHO_IS
    assert turn.answer.grounded
    assert turn.answer.citation_coverage == 1.0
    assert turn.suggestions
    assert turn.context_span_ids


async def test_ask_does_not_leak_future_spans() -> None:
    svc = _service()
    pos = ReadingPosition(book_id="b", beat_index=8)
    turn = await svc.ask("b", "Who is the Duke?", pos)
    # The villain span (beat 9) must not be in the assembled context.
    assert "canon:char_villain" not in turn.context_span_ids
    # No suggestion may point at the future villain either.
    assert all(s.about_entity_key != "char_villain" for s in turn.suggestions)


async def test_ask_records_conversation() -> None:
    mem = ConversationMemory(token_budget=10_000)
    svc = _service(memory=mem)
    pos = ReadingPosition(book_id="b", beat_index=8)
    await svc.ask("b", "Who is Elsa?", pos, conversation_id="conv1")
    hist = await mem.recall("conv1")
    assert len(hist) == 2
    assert hist[0].content == "Who is Elsa?"


async def test_follow_up_uses_history() -> None:
    mem = ConversationMemory(token_budget=10_000)
    chat = FakeChat(answer="She is on the mountain [1].", citations=[1])
    svc = _service(chat=chat, memory=mem)
    pos = ReadingPosition(book_id="b", beat_index=8)
    await svc.ask("b", "Who is Elsa?", pos, conversation_id="c")
    await svc.ask("b", "Where is she now?", pos, conversation_id="c")
    # The second call's prompt should include the prior turn as history.
    second_call_msgs = chat.calls[-1]["messages"]
    roles = [m["role"] for m in second_call_msgs]
    assert roles.count("user") >= 2  # history user turn + current user turn


async def test_recap_intent_routed() -> None:
    svc = _service()
    pos = ReadingPosition(book_id="b", beat_index=8)
    turn = await svc.ask("b", "What has happened so far?", pos)
    assert turn.intent is AssistantIntent.RECAP


async def test_ask_stream_yields_done_with_suggestions() -> None:
    svc = _service()
    pos = ReadingPosition(book_id="b", beat_index=8)
    deltas: list[StreamDelta] = []
    async for d in svc.ask_stream("b", "Who is Elsa?", pos):
        deltas.append(d)
    done = deltas[-1]
    assert done.type == "done"
    assert done.answer is not None and done.answer.grounded
    assert done.suggestions  # suggestions attached to the terminal delta


async def test_unread_position_reveals_nothing_meaningful() -> None:
    # A reader at the very start (no beat) gets ceiling 0; ordinal-0 spans only.
    svc = _service()
    pos = ReadingPosition(book_id="b")  # unset → conservative
    turn = await svc.ask("b", "Who is the Duke?", pos)
    # All visible spans start at ordinal >= 1, so nothing is in context → refusal.
    assert turn.answer.refused or not turn.context_span_ids
