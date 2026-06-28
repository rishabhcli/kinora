"""Unit tests for the classifier seam: the deterministic keyword/regex fake."""

from __future__ import annotations

import pytest

from app.moderation.classifier import (
    ContentClassifier,
    KeywordClassifier,
    _labels_from_model,
    _merge_labels,
    _sample,
    build_default_classifier,
)
from app.moderation.contracts import ContentLabel, Surface
from app.moderation.taxonomy import ModerationCategory, Severity


@pytest.mark.asyncio
async def test_clean_text_returns_safe() -> None:
    kc = KeywordClassifier()
    res = await kc.classify_text("a calm walk in the park", surface=Surface.INGEST_TEXT)
    assert len(res.labels) == 1
    assert res.labels[0].category is ModerationCategory.SAFE
    assert res.positive_labels() == []
    assert res.max_severity is Severity.NONE


@pytest.mark.asyncio
async def test_csam_keyword_fires_critical() -> None:
    kc = KeywordClassifier()
    res = await kc.classify_text("this is csam content", surface=Surface.INGEST_TEXT)
    cats = {lab.category for lab in res.positive_labels()}
    assert ModerationCategory.SEXUAL_MINORS in cats


@pytest.mark.asyncio
async def test_ssn_regex_detects_pii() -> None:
    kc = KeywordClassifier()
    res = await kc.classify_text("my number is 123-45-6789", surface=Surface.INGEST_TEXT)
    cats = {lab.category for lab in res.positive_labels()}
    assert ModerationCategory.PII in cats


@pytest.mark.asyncio
async def test_email_regex_detects_pii() -> None:
    kc = KeywordClassifier()
    res = await kc.classify_text("write to alice@example.com", surface=Surface.INGEST_TEXT)
    assert ModerationCategory.PII in {lab.category for lab in res.positive_labels()}


@pytest.mark.asyncio
async def test_frame_labels_are_injectable() -> None:
    frame = b"\x89PNG-frame-bytes"
    label = ContentLabel.of(ModerationCategory.GORE, 0.9)
    kc = KeywordClassifier(frame_labels={frame: [label]})
    res = await kc.classify_frames([frame], surface=Surface.CLIP)
    assert res.positive_labels()[0].category is ModerationCategory.GORE


@pytest.mark.asyncio
async def test_unknown_frames_are_safe() -> None:
    kc = KeywordClassifier()
    res = await kc.classify_frames([b"unknown"], surface=Surface.KEYFRAME)
    assert res.labels[0].category is ModerationCategory.SAFE


@pytest.mark.asyncio
async def test_empty_frames_are_safe() -> None:
    kc = KeywordClassifier()
    res = await kc.classify_frames([], surface=Surface.CLIP)
    assert res.labels[0].category is ModerationCategory.SAFE


def test_keyword_classifier_satisfies_protocol() -> None:
    assert isinstance(KeywordClassifier(), ContentClassifier)


def test_build_default_classifier_offline_is_keyword() -> None:
    # With no providers, the default classifier is the offline keyword fake.
    clf = build_default_classifier(None, settings=None)
    assert isinstance(clf, KeywordClassifier)


def test_labels_from_model_is_defensive() -> None:
    # Garbage in -> no crash; unknown category folds to OTHER; score clamps.
    labels = _labels_from_model(
        {
            "labels": [
                {"category": "gore", "score": 0.9},
                {"category": "made_up", "score": 2.0},
                {"category": "sexual", "score": "nan"},
                "not a dict",
            ]
        }
    )
    cats = {lab.category for lab in labels}
    assert ModerationCategory.GORE in cats
    assert ModerationCategory.OTHER in cats
    assert all(0.0 <= lab.score <= 1.0 for lab in labels)


def test_labels_from_model_handles_non_dict() -> None:
    assert _labels_from_model("garbage") == []
    assert _labels_from_model({"labels": "garbage"}) == []


def test_merge_labels_keeps_highest_score_per_category() -> None:
    a = [ContentLabel.of(ModerationCategory.GORE, 0.3)]
    b = [ContentLabel.of(ModerationCategory.GORE, 0.9)]
    merged = _merge_labels(a, b)
    assert len(merged) == 1
    assert merged[0].score == pytest.approx(0.9)


def test_sample_bounds_frame_count() -> None:
    frames = [bytes([i]) for i in range(10)]
    assert _sample(frames, 4) == [frames[0], frames[2], frames[5], frames[7]]
    assert _sample(frames[:3], 4) == frames[:3]  # fewer than k -> all
