"""The assistant eval harness — faithfulness, citation-coverage, spoiler-safety.

§13 measures the system honestly with pre-registered metrics; the reader
assistant gets the same treatment. Given a book's spans and a set of synthetic
Q&A cases, this harness scores the assistant on three axes a grounded RAG system
lives or dies by:

* **citation coverage** — share of answer sentences carrying a valid citation
  (the headline faithfulness signal; reuses the grounding guard);
* **faithfulness** — share of answers whose every cited span actually contains
  the answer's key terms (a citation that doesn't support its sentence is worse
  than no citation), measured by lexical overlap so it needs no model judge;
* **spoiler safety** — zero answers may cite or quote a span past the case's
  reading position; a single leak fails the case.

The harness is pure and offline. It can run against a real :class:`AssistantService`
(injected) *or* score a precomputed answer, so tests exercise it with a fake
service and zero network. It also generates **synthetic Q&A** deterministically
from a book's spans (a who-is per canon entity, a recap, an explain) so a CI run
has cases without a hand-authored gold set.
"""

from __future__ import annotations

import statistics
from collections.abc import Sequence
from dataclasses import dataclass, field

from app.assistant.types import (
    Answer,
    AssistantIntent,
    ReadingPosition,
    RetrievedSpan,
    SourceKind,
)
from app.memory.retrieval import jaccard, tokenize


@dataclass(frozen=True, slots=True)
class EvalCase:
    """One synthetic Q&A case: a question at a position with allowed sources."""

    question: str
    position: ReadingPosition
    intent: AssistantIntent = AssistantIntent.GENERAL
    #: span_ids that are *legitimately* available at this position (the ground).
    allowed_span_ids: frozenset[str] = field(default_factory=frozenset)
    #: span_ids that are FUTURE relative to the position (must never be cited).
    future_span_ids: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True, slots=True)
class CaseScore:
    """The per-case scores."""

    question: str
    citation_coverage: float
    faithfulness: float
    spoiler_safe: bool
    refused: bool


@dataclass(frozen=True, slots=True)
class EvalReport:
    """Aggregate over all cases (mean + worst-case spoiler safety)."""

    n: int
    mean_citation_coverage: float
    mean_faithfulness: float
    spoiler_safety_rate: float
    refusal_rate: float
    cases: list[CaseScore] = field(default_factory=list)

    def passes(
        self, *, min_coverage: float = 0.6, min_faithfulness: float = 0.5
    ) -> bool:
        """True when the run clears the (caller-chosen) thresholds AND is spoiler-clean."""
        return (
            self.spoiler_safety_rate >= 1.0
            and self.mean_citation_coverage >= min_coverage
            and self.mean_faithfulness >= min_faithfulness
        )


def generate_cases(
    spans: Sequence[RetrievedSpan], position: ReadingPosition
) -> list[EvalCase]:
    """Deterministically synthesize Q&A cases from a book's spans at a position.

    Splits spans into "allowed" (ordinal <= position beat) and "future" so each
    case carries the spoiler ground truth. Produces a who-is per visible canon
    entity, a recap, and an explain on the top visible page passage.
    """
    ceiling = position.beat_index if position.beat_index is not None else (
        1 << 62 if position.allow_full_book else 0
    )
    allowed = [s for s in spans if s.ordinal <= ceiling]
    future = frozenset(s.span_id for s in spans if s.ordinal > ceiling)
    allowed_ids = frozenset(s.span_id for s in allowed)

    cases: list[EvalCase] = []
    for span in allowed:
        if span.kind == SourceKind.CANON:
            name = (span.meta or {}).get("name") or span.locator
            cases.append(
                EvalCase(
                    question=f"Who is {name}?",
                    position=position,
                    intent=AssistantIntent.WHO_IS,
                    allowed_span_ids=allowed_ids,
                    future_span_ids=future,
                )
            )
    cases.append(
        EvalCase(
            question="What has happened so far?",
            position=position,
            intent=AssistantIntent.RECAP,
            allowed_span_ids=allowed_ids,
            future_span_ids=future,
        )
    )
    pages = [s for s in allowed if s.kind == SourceKind.PAGE]
    if pages:
        cases.append(
            EvalCase(
                question="Explain what is happening in this passage.",
                position=position,
                intent=AssistantIntent.EXPLAIN,
                allowed_span_ids=allowed_ids,
                future_span_ids=future,
            )
        )
    return cases


def faithfulness(answer: Answer, context_by_id: dict[str, RetrievedSpan]) -> float:
    """Lexical faithfulness: do the cited spans actually support the answer?

    For each cited span, measure the token overlap between the answer's prose and
    the span's text; a citation is "supportive" when the overlap clears a small
    floor. Returns the share of citations that are supportive (1.0 when there are
    no citations and the answer didn't claim anything, else 0.0 for a bare claim).
    """
    if answer.refused:
        return 1.0  # a refusal makes no claims → vacuously faithful
    if not answer.citations:
        return 0.0
    answer_tokens = tokenize(answer.text)
    supportive = 0
    for cite in answer.citations:
        span = context_by_id.get(cite.span_id)
        if span is None:
            continue
        overlap = len(answer_tokens & tokenize(span.text))
        if overlap >= 1 or jaccard(answer.text, span.text) > 0.02:
            supportive += 1
    return supportive / len(answer.citations)


def spoiler_safe(answer: Answer, case: EvalCase) -> bool:
    """True when no citation points at a future span (a hard safety gate)."""
    cited = {c.span_id for c in answer.citations}
    return not (cited & case.future_span_ids)


def score_case(
    case: EvalCase, answer: Answer, context_by_id: dict[str, RetrievedSpan]
) -> CaseScore:
    """Score one answered case on all three axes."""
    return CaseScore(
        question=case.question,
        citation_coverage=answer.citation_coverage,
        faithfulness=faithfulness(answer, context_by_id),
        spoiler_safe=spoiler_safe(answer, case),
        refused=answer.refused,
    )


def aggregate(scores: Sequence[CaseScore]) -> EvalReport:
    """Aggregate per-case scores into a report (means + safety/refusal rates)."""
    if not scores:
        return EvalReport(
            n=0,
            mean_citation_coverage=0.0,
            mean_faithfulness=0.0,
            spoiler_safety_rate=1.0,
            refusal_rate=0.0,
            cases=[],
        )
    # Coverage/faithfulness exclude refusals from the means (a refusal isn't a
    # graded answer); a run that's all refusals reports 0.0 means and is caught
    # by the refusal_rate instead.
    graded = [s for s in scores if not s.refused]
    cov = statistics.fmean(s.citation_coverage for s in graded) if graded else 0.0
    faith = statistics.fmean(s.faithfulness for s in graded) if graded else 0.0
    safe_rate = sum(1 for s in scores if s.spoiler_safe) / len(scores)
    refusal_rate = sum(1 for s in scores if s.refused) / len(scores)
    return EvalReport(
        n=len(scores),
        mean_citation_coverage=round(cov, 4),
        mean_faithfulness=round(faith, 4),
        spoiler_safety_rate=round(safe_rate, 4),
        refusal_rate=round(refusal_rate, 4),
        cases=list(scores),
    )


__all__ = [
    "CaseScore",
    "EvalCase",
    "EvalReport",
    "aggregate",
    "faithfulness",
    "generate_cases",
    "score_case",
    "spoiler_safe",
]
