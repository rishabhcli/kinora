"""Unit tests for pacing-aware tempo classification + shot density (no network)."""

from __future__ import annotations

from app.agents.comprehension.pacing import (
    classify_tempo,
    density_multiplier,
    duration_bias,
    words_per_shot_for,
)
from app.agents.contracts import SceneTempo


def test_summary_tempo_on_long_span() -> None:
    a = classify_tempo("The war dragged on for many years, and the kingdom grew weary.")
    assert a.tempo is SceneTempo.SUMMARY


def test_scene_tempo_on_dialogue() -> None:
    a = classify_tempo('"Run!" she screamed. "They are coming!"')
    assert a.tempo is SceneTempo.SCENE


def test_scene_tempo_on_action() -> None:
    a = classify_tempo("He leapt the gap, grabbed the ledge, and hauled himself up.")
    assert a.tempo is SceneTempo.SCENE


def test_pause_tempo_on_description() -> None:
    a = classify_tempo(
        "The valley stretched silent below, its frost-lined meadows still and "
        "motionless under a pale and watchful winter sky that loomed overhead."
    )
    assert a.tempo is SceneTempo.PAUSE


def test_ellipsis_tempo_on_time_jump() -> None:
    a = classify_tempo("The next morning, the snow had finally stopped.")
    assert a.tempo is SceneTempo.ELLIPSIS


def test_density_multiplier_ordering() -> None:
    # A dramatised scene is denser (more shots) than a summary or ellipsis.
    assert density_multiplier(SceneTempo.SCENE) > density_multiplier(SceneTempo.SUMMARY)
    assert density_multiplier(SceneTempo.SUMMARY) > density_multiplier(SceneTempo.ELLIPSIS)


def test_words_per_shot_inverse_of_density() -> None:
    base = 60
    # SCENE keeps the baseline; SUMMARY packs more words into one shot.
    assert words_per_shot_for(SceneTempo.SCENE, base) == base
    assert words_per_shot_for(SceneTempo.SUMMARY, base) > base
    assert words_per_shot_for(SceneTempo.ELLIPSIS, base) >= words_per_shot_for(
        SceneTempo.SUMMARY, base
    )


def test_duration_bias_pause_lingers() -> None:
    assert duration_bias(SceneTempo.PAUSE) > duration_bias(SceneTempo.SCENE)
    assert duration_bias(SceneTempo.SCENE) == 1.0


def test_empty_text_neutral_scene() -> None:
    assert classify_tempo("").tempo is SceneTempo.SCENE
