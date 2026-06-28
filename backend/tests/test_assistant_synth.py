"""Tests for the answer synthesizer (chat seam faked; grounding applied)."""

from __future__ import annotations

from app.assistant.context import AssembledContext, ContextAssembler
from app.assistant.synth import AnswerSynthesizer
from app.assistant.types import RetrievedSpan, SourceKind, StreamDelta
from tests.assistant_fakes import FakeChat


def _ctx(*texts: str) -> AssembledContext:
    spans = [
        RetrievedSpan(
            span_id=f"s{i}", kind=SourceKind.PAGE, text=t, score=1.0 - i * 0.1, locator=f"p.{i}"
        )
        for i, t in enumerate(texts, start=1)
    ]
    return ContextAssembler().assemble(spans)


async def test_synthesize_grounded_answer() -> None:
    ctx = _ctx("Elsa has a platinum braid and an ice-blue gown.")
    chat = FakeChat(answer="Elsa has a platinum braid [1].", citations=[1])
    synth = AnswerSynthesizer(chat, "model-x")
    answer = await synth.synthesize("Who is Elsa?", ctx)
    assert answer.grounded
    assert answer.citation_coverage == 1.0
    assert {c.marker for c in answer.citations} == {1}


async def test_empty_context_refuses_without_calling_model() -> None:
    ctx = ContextAssembler().assemble([])
    chat = FakeChat()
    synth = AnswerSynthesizer(chat, "model-x")
    answer = await synth.synthesize("Who is X?", ctx)
    assert answer.refused
    assert chat.calls == []  # short-circuited, no model call


async def test_model_refusal_is_propagated() -> None:
    ctx = _ctx("Some passage about the mountain.")
    chat = FakeChat(refused=True)
    synth = AnswerSynthesizer(chat, "model-x")
    answer = await synth.synthesize("Who is the villain?", ctx)
    assert answer.refused
    assert not answer.grounded


async def test_model_error_degrades_to_refusal() -> None:
    ctx = _ctx("A passage.")
    chat = FakeChat(raise_error=True)
    synth = AnswerSynthesizer(chat, "model-x")
    answer = await synth.synthesize("Anything?", ctx)
    assert answer.refused


async def test_invented_citation_lowers_coverage() -> None:
    ctx = _ctx("Elsa has a braid.")
    chat = FakeChat(answer="Elsa has a braid [1]. She rules a kingdom [5].", citations=[1, 5])
    synth = AnswerSynthesizer(chat, "model-x")
    answer = await synth.synthesize("Who is Elsa?", ctx)
    # [5] is invalid → only [1] survives, coverage < 1.
    assert {c.marker for c in answer.citations} == {1}
    assert answer.citation_coverage < 1.0


async def test_stream_emits_tokens_then_done() -> None:
    ctx = _ctx("Elsa has a platinum braid.")
    chat = FakeChat(answer="Elsa has a platinum braid [1].")
    synth = AnswerSynthesizer(chat, "model-x")
    deltas: list[StreamDelta] = []
    async for d in synth.stream("Who is Elsa?", ctx):
        deltas.append(d)
    types = [d.type for d in deltas]
    assert types[-1] == "done"
    assert "citations" in types
    assert any(d.type == "token" for d in deltas)
    final = deltas[-1].answer
    assert final is not None
    # The streamed prose reassembles into a grounded answer.
    assert final.citation_coverage == 1.0


async def test_stream_empty_context_refuses() -> None:
    ctx = ContextAssembler().assemble([])
    chat = FakeChat()
    synth = AnswerSynthesizer(chat, "model-x")
    deltas = [d async for d in synth.stream("Q?", ctx)]
    assert deltas[-1].type == "done"
    assert deltas[-1].answer is not None
    assert deltas[-1].answer.refused
    assert chat.calls == []
