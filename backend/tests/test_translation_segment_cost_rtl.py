"""Unit tests for segmentation, cost/batching, RTL handling, and hashing."""

from __future__ import annotations

from app.translation.cost import (
    CostLedger,
    batch_requests,
    estimate_tokens,
    predict_cost,
)
from app.translation.hashing import (
    artifact_key,
    source_content_hash,
    translation_key,
)
from app.translation.provider import ProviderRequest
from app.translation.rtl import (
    has_rtl_characters,
    isolate_ltr_runs,
    prepare_rtl_segment,
    strip_controls,
)
from app.translation.segment import (
    join_segments,
    segment_text,
    split_paragraphs,
    split_sentences,
)
from app.translation.types import ContentKind, TranslationCost

# -- segmentation ------------------------------------------------------------ #


def test_split_sentences_basic() -> None:
    out = split_sentences("Hello there. How are you? I am fine!")
    assert out == ["Hello there.", "How are you?", "I am fine!"]


def test_split_sentences_respects_abbreviation() -> None:
    out = split_sentences("Dr. Smith went home. He slept.")
    assert out == ["Dr. Smith went home.", "He slept."]


def test_split_sentences_does_not_break_inside_placeholder() -> None:
    out = split_sentences("See {file.name} now. Done.")
    assert out == ["See {file.name} now.", "Done."]


def test_split_paragraphs() -> None:
    assert split_paragraphs("Para one.\n\nPara two.") == ["Para one.", "Para two."]


def test_segment_text_ids_and_kind() -> None:
    segs = segment_text("One. Two.", base_id="p1", kind=ContentKind.NARRATION)
    assert [s.id for s in segs] == ["p1.0", "p1.1"]
    assert all(s.kind is ContentKind.NARRATION for s in segs)


def test_segment_text_whole_granularity() -> None:
    segs = segment_text("a. b. c.", base_id="p", granularity="whole")
    assert len(segs) == 1


def test_join_segments() -> None:
    assert join_segments(["a", "", "b"], separator=" ") == "a b"


# -- cost + batching --------------------------------------------------------- #


def test_estimate_tokens_monotonic() -> None:
    assert estimate_tokens("a") >= 1
    assert estimate_tokens("a" * 40) > estimate_tokens("a")


def test_batch_requests_bounds() -> None:
    reqs = [ProviderRequest(f"text {i}", "en", "fr") for i in range(70)]
    batches = batch_requests(reqs, max_batch_size=32)
    assert all(len(b) <= 32 for b in batches)
    assert sum(len(b) for b in batches) == 70


def test_batch_requests_token_bound() -> None:
    big = ProviderRequest("x" * 40000, "en", "fr")
    small = ProviderRequest("y", "en", "fr")
    batches = batch_requests([big, small], max_batch_tokens=100)
    # The oversized request ships alone; the small one in its own batch.
    assert len(batches) == 2


def test_cost_ledger_records_and_summarizes() -> None:
    ledger = CostLedger()
    ledger.record(
        TranslationCost(input_tokens=10, output_tokens=12, provider_calls=1, segments=2),
        target_lang="fr",
    )
    ledger.record_cache_hit(target_lang="fr")
    summary = ledger.summary()
    assert summary["input_tokens"] == 10
    assert summary["cache_hits"] == 1
    by_language = summary["by_language"]
    assert isinstance(by_language, dict)
    assert "fr" in by_language


def test_predict_cost() -> None:
    reqs = [ProviderRequest("hello world", "en", "fr") for _ in range(3)]
    cost = predict_cost(reqs)
    assert cost.segments == 3
    assert cost.input_tokens > 0
    assert cost.output_tokens >= cost.input_tokens  # 1.3x expansion


# -- RTL --------------------------------------------------------------------- #


def test_strip_controls_idempotent() -> None:
    dirty = "a‫b‬c"
    assert strip_controls(dirty) == "abc"
    assert strip_controls(strip_controls(dirty)) == "abc"


def test_has_rtl_characters() -> None:
    assert has_rtl_characters("مرحبا")
    assert not has_rtl_characters("hello")


def test_isolate_ltr_runs_wraps_embedded_latin() -> None:
    out = isolate_ltr_runs("مرحبا Kinora 2024")
    assert "⁨" in out  # FSI
    assert "⁩" in out  # PDI


def test_prepare_rtl_segment_ltr_noop() -> None:
    # LTR target: only strips stray controls, no isolation.
    assert prepare_rtl_segment("hello", "en") == "hello"


def test_prepare_rtl_segment_rtl_isolates() -> None:
    out = prepare_rtl_segment("مرحبا Kinora", "ar")
    assert "⁨Kinora⁩" in out


# -- hashing ----------------------------------------------------------------- #


def test_translation_key_deterministic_and_sensitive() -> None:
    def key(
        *,
        source_text: str = "Hello",
        source_lang: str = "en",
        target_lang: str = "fr",
        content_kind: ContentKind = ContentKind.PAGE_TEXT,
        glossary_version: int = 0,
    ) -> str:
        return translation_key(
            source_text=source_text,
            source_lang=source_lang,
            target_lang=target_lang,
            content_kind=content_kind,
            glossary_version=glossary_version,
        )

    k1 = key()
    assert k1 == key()
    # Each component changes the key.
    assert k1 != key(target_lang="de")
    assert k1 != key(glossary_version=1)
    assert k1 != key(content_kind=ContentKind.NARRATION)


def test_source_content_hash_stable() -> None:
    assert source_content_hash("abc") == source_content_hash("abc")
    assert source_content_hash("abc") != source_content_hash("abd")


def test_artifact_key_namespaced() -> None:
    a = artifact_key(book_id="b1", target_lang="fr", content_kind=ContentKind.PAGE_TEXT)
    b = artifact_key(book_id="b1", target_lang="fr", content_kind=ContentKind.NARRATION)
    assert a != b
    assert len(a) == 32
