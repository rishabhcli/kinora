"""Tests for the assistant eval harness (faithfulness / coverage / spoiler-safety)."""

from __future__ import annotations

from app.assistant.eval import (
    EvalCase,
    aggregate,
    faithfulness,
    generate_cases,
    score_case,
    spoiler_safe,
)
from app.assistant.service import AssistantService
from app.assistant.synth import AnswerSynthesizer
from app.assistant.types import (
    Answer,
    Citation,
    ReadingPosition,
    RetrievedSpan,
    SourceKind,
)
from tests.assistant_fakes import FakeChat, FakeEmbedder, FakeReadModel, make_spans


def test_generate_cases_splits_allowed_and_future() -> None:
    spans = make_spans()
    pos = ReadingPosition(book_id="b", beat_index=8)
    cases = generate_cases(spans, pos)
    assert cases
    # The future villain span is in every case's future set, never the allowed set.
    for case in cases:
        assert "canon:char_villain" in case.future_span_ids
        assert "canon:char_villain" not in case.allowed_span_ids


def test_faithfulness_rewards_supportive_citation() -> None:
    span = RetrievedSpan(
        span_id="s1", kind=SourceKind.PAGE, text="Elsa has a platinum braid.", score=1.0
    )
    answer = Answer(
        text="Elsa has a braid [1].",
        citations=[Citation(marker=1, span_id="s1", kind=SourceKind.PAGE)],
        citation_coverage=1.0,
    )
    assert faithfulness(answer, {"s1": span}) == 1.0


def test_faithfulness_penalizes_unsupported_citation() -> None:
    span = RetrievedSpan(
        span_id="s1", kind=SourceKind.PAGE, text="Completely unrelated words zzz qqq.", score=1.0
    )
    answer = Answer(
        text="Elsa flies a dragon [1].",
        citations=[Citation(marker=1, span_id="s1", kind=SourceKind.PAGE)],
        citation_coverage=1.0,
    )
    assert faithfulness(answer, {"s1": span}) == 0.0


def test_faithfulness_refusal_is_vacuously_faithful() -> None:
    answer = Answer(text="refused", refused=True)
    assert faithfulness(answer, {}) == 1.0


def test_spoiler_safe_flags_future_citation() -> None:
    case = EvalCase(
        question="q",
        position=ReadingPosition(book_id="b", beat_index=8),
        future_span_ids=frozenset({"future1"}),
    )
    bad = Answer(
        text="leak [1]",
        citations=[Citation(marker=1, span_id="future1", kind=SourceKind.CANON)],
    )
    good = Answer(
        text="ok [1]",
        citations=[Citation(marker=1, span_id="allowed1", kind=SourceKind.PAGE)],
    )
    assert not spoiler_safe(bad, case)
    assert spoiler_safe(good, case)


def test_aggregate_means_and_rates() -> None:
    case = EvalCase(question="q", position=ReadingPosition(book_id="b", beat_index=1))
    span = RetrievedSpan(span_id="s1", kind=SourceKind.PAGE, text="braid here", score=1.0)
    answer = Answer(
        text="braid here [1].",
        citations=[Citation(marker=1, span_id="s1", kind=SourceKind.PAGE)],
        citation_coverage=1.0,
    )
    score = score_case(case, answer, {"s1": span})
    report = aggregate([score])
    assert report.n == 1
    assert report.mean_citation_coverage == 1.0
    assert report.spoiler_safety_rate == 1.0
    assert report.refusal_rate == 0.0


def test_empty_aggregate_is_safe() -> None:
    report = aggregate([])
    assert report.n == 0
    assert report.spoiler_safety_rate == 1.0


async def test_full_eval_run_against_fake_service_is_spoiler_clean() -> None:
    # Build a service whose chat cites the first context span faithfully.
    spans = make_spans()
    rm = FakeReadModel(spans)
    chat = FakeChat(answer="Elsa has a platinum braid and an ice-blue gown [1].", citations=[1])
    svc = AssistantService(rm, AnswerSynthesizer(chat, "m"), embedder=FakeEmbedder())

    pos = ReadingPosition(book_id="b", beat_index=8)
    cases = generate_cases(spans, pos)
    scores = []
    for case in cases:
        turn = await svc.ask("b", case.question, case.position)
        # Build the context map from the spans the service actually retrieved.
        ctx_map = {s.span_id: s for s in spans}
        scores.append(score_case(case, turn.answer, ctx_map))

    report = aggregate(scores)
    # The hard invariant: no answer ever cited a future span.
    assert report.spoiler_safety_rate == 1.0
