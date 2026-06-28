"""Unit tests for the heuristic language detector."""

from __future__ import annotations

import pytest

from app.translation.detect import HeuristicDetector, detect_language


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("the quick brown fox jumps over the lazy dog and runs", "en"),
        ("le chat est sur la table et le chien dans la maison", "fr"),
        ("el gato está en la mesa y el perro en la casa con un libro", "es"),
        ("der Hund und die Katze sind in dem Haus mit dem Buch", "de"),
        ("Привет мир это книга на русском языке очень хорошо", "ru"),
        ("これは日本語のテキストです", "ja"),
        ("이것은 한국어 텍스트입니다", "ko"),
        ("هذا نص باللغة العربية", "ar"),
        ("זהו טקסט בעברית", "he"),
    ],
)
def test_detects_expected_language(text: str, expected: str) -> None:
    detection = detect_language(text)
    assert detection.language.tag.split("-")[0] == expected or detection.language.tag == expected


def test_empty_text_returns_default() -> None:
    d = detect_language("", default="fr")
    assert d.language.tag == "fr"
    assert d.method == "default"


def test_script_detection_is_confident() -> None:
    d = detect_language("Привет это длинный русский текст для проверки")
    assert d.method == "script"
    assert d.confidence > 0.6


def test_japanese_kana_beats_han() -> None:
    # Mixed kanji + kana should resolve to Japanese, not Chinese.
    d = detect_language("これは漢字とひらがなの文章です")
    assert d.language.tag == "ja"


def test_low_signal_falls_back_to_default() -> None:
    d = detect_language("xyz qrs", default="en")
    assert d.language.tag == "en"
    assert d.method == "default"


def test_detector_is_injectable_and_deterministic() -> None:
    det = HeuristicDetector(stopword_floor=0.05)
    a = det.detect("the cat and the dog")
    b = det.detect("the cat and the dog")
    assert a == b
