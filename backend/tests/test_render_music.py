"""Unit tests for music scoring: deterministic mood classification, palette nudge,
and intensity overrides. Pure — no ffmpeg, no audio, no network."""

from __future__ import annotations

from app.render.music import (
    Mood,
    classify_mood,
    cue_for_mood,
    score_mood_keywords,
    score_scene,
)


def test_classify_mood_picks_dominant_keyword() -> None:
    assert classify_mood("a tense, menacing confrontation in the dark") is Mood.TENSE
    assert classify_mood("a calm, quiet, peaceful morning") is Mood.CALM
    assert classify_mood("a triumphant, glorious victory") is Mood.TRIUMPHANT
    assert classify_mood("grief and sorrow at the funeral") is Mood.SOMBRE


def test_classify_mood_defaults_to_neutral() -> None:
    assert classify_mood(None) is Mood.NEUTRAL
    assert classify_mood("") is Mood.NEUTRAL
    assert classify_mood("the cat sat on the mat") is Mood.NEUTRAL  # no mood words


def test_classify_mood_is_deterministic_on_ties() -> None:
    # One word from CALM and one from TENSE → deterministic tie-break, never random.
    text = "quiet dread"
    first = classify_mood(text)
    assert all(classify_mood(text) is first for _ in range(5))


def test_score_mood_keywords_reports_hits() -> None:
    scores = score_mood_keywords("a tense, urgent, menacing chase")
    assert scores.scores[Mood.TENSE] >= 3
    assert scores.best is Mood.TENSE


def test_score_scene_palette_nudges_intensity() -> None:
    base = cue_for_mood(Mood.CALM)
    warm = score_scene(mood_text="calm", palette="warm")
    cool = score_scene(mood_text="calm", palette="cool")
    assert warm.intensity > base.intensity
    assert cool.intensity < base.intensity
    # The mood/pitch identity is unchanged — only intensity moved.
    assert warm.mood is Mood.CALM and warm.chord_hz == base.chord_hz


def test_score_scene_intensity_override_wins() -> None:
    cue = score_scene(mood_text="tense", palette="warm", intensity_override=0.9)
    assert cue.intensity == 0.9
    # Override is clamped to [0, 1].
    assert score_scene(mood_text="calm", intensity_override=5.0).intensity == 1.0
    assert score_scene(mood_text="calm", intensity_override=-1.0).intensity == 0.0


def test_minor_modes_for_dark_moods() -> None:
    assert cue_for_mood(Mood.TENSE).mode == "minor"
    assert cue_for_mood(Mood.SOMBRE).mode == "minor"
    assert cue_for_mood(Mood.TRIUMPHANT).mode == "major"


def test_every_mood_has_a_real_cue() -> None:
    for mood in Mood:
        cue = cue_for_mood(mood)
        assert cue.root_hz > 0
        assert cue.tempo_bpm > 0
        assert 0.0 <= cue.intensity <= 1.0
        assert len(cue.chord_hz) == 3
