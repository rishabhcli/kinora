"""Unit tests for director-note → preference inference and plain-language priors
(kinora.md §8.6). Pure functions, no network, no DB."""

from __future__ import annotations

from app.memory.prefs_service import PreferencePrior, PreferencePriors
from app.memory.prefs_signals import (
    APPLY_THRESHOLD,
    BIAS_CLAMP,
    DECAY_HALF_LIFE_DAYS,
    SIGNAL_STEP,
    applied_value,
    camera_overrides,
    categorical,
    decay_factor,
    describe,
    grade_for,
    infer_signals,
    infer_signals_from_changes,
    is_applied,
    merge_bias,
    preferences_payload,
    prompt_hints,
)


def _priors(**by_kind: float) -> PreferencePriors:
    """Build aggregated priors from ``kind=bias`` pairs (weight defaults sane)."""
    return PreferencePriors(
        priors={
            kind: PreferencePrior(kind=kind, value={"bias": bias}, weight=3.0, signals=1)
            for kind, bias in by_kind.items()
        }
    )


# --- inference --------------------------------------------------------------- #


def test_infer_slower_is_negative_pacing() -> None:
    assert infer_signals("this is too fast, slow it down") == [("pacing", -1)]


def test_infer_faster_is_positive_pacing() -> None:
    assert infer_signals("make it snappier, pick up the pace") == [("pacing", 1)]


def test_infer_warmer_is_positive_palette() -> None:
    assert infer_signals("give it a warmer palette") == [("palette", 1)]


def test_infer_wider_is_positive_composition() -> None:
    assert infer_signals("pull back, more of the scene") == [("composition", 1)]


def test_infer_closer_is_negative_composition() -> None:
    assert infer_signals("get closer, tighter on her face") == [("composition", -1)]


def test_infer_multiple_axes_in_one_note() -> None:
    signals = dict(infer_signals("slower and warmer, please"))
    assert signals == {"pacing": -1, "palette": 1}


def test_infer_ambiguous_axis_yields_no_signal() -> None:
    # Pushed both ways on the same axis -> no clear lesson.
    assert ("palette", 1) not in infer_signals("warmer highlights but cooler shadows")
    assert ("palette", -1) not in infer_signals("warmer highlights but cooler shadows")


def test_infer_unrelated_note_is_empty() -> None:
    assert infer_signals("the character looks great here") == []


def test_infer_from_canon_changes() -> None:
    changes = {"appearance": {"description": "a warmer, golden coat"}}
    assert infer_signals_from_changes(changes) == [("palette", 1)]


# --- bias arithmetic --------------------------------------------------------- #


def test_merge_bias_accumulates_and_clamps() -> None:
    bias = 0.0
    for _ in range(3):
        bias = merge_bias(bias, -1)
    assert bias == -0.9  # three "slower" signals at the ±0.3 step
    # Far past the clamp stays pinned.
    for _ in range(20):
        bias = merge_bias(bias, -1)
    assert bias == -BIAS_CLAMP


def test_merge_bias_opposing_signals_cancel() -> None:
    bias = merge_bias(merge_bias(0.0, -1), 1)
    assert bias == 0.0


# --- priors -> camera/prompt defaults ---------------------------------------- #


def test_categorical_only_applies_past_threshold() -> None:
    assert categorical("pacing", -SIGNAL_STEP) is None  # one note: too weak
    assert categorical("pacing", -0.9) == "slow"
    assert categorical("composition", 0.9) == "wide"


def test_camera_overrides_applies_strong_priors() -> None:
    overrides = camera_overrides(_priors(pacing=-0.9, composition=0.6))
    assert overrides == {"speed": "slow", "shot_size": "wide"}


def test_camera_overrides_ignores_weak_priors() -> None:
    assert camera_overrides(_priors(pacing=-0.3)) == {}


def test_camera_overrides_skips_axes_addressed_now() -> None:
    # An explicit ask this session wins over the learned default.
    assert camera_overrides(_priors(pacing=-0.9), skip=frozenset({"pacing"})) == {}


def test_prompt_hints_for_palette() -> None:
    assert prompt_hints(_priors(palette=0.9)) == ["a warmer color palette"]
    assert prompt_hints(_priors(palette=0.3)) == []


def test_preferences_payload_is_compact_directives() -> None:
    payload = preferences_payload(_priors(pacing=-0.9, palette=0.9))
    assert payload == {"pacing": "slower camera moves", "palette": "a warmer palette"}


# --- plain-language description ---------------------------------------------- #


def test_describe_applied_pacing() -> None:
    prior = PreferencePrior(kind="pacing", value={"bias": -0.9}, weight=3.0, signals=1)
    label, detail = describe(prior)
    assert label == "You prefer slower, lingering shots"
    assert "3 director edits" in detail
    assert is_applied(prior) is True
    assert applied_value(prior) == "slow"


def test_describe_palette_shows_signed_magnitude() -> None:
    prior = PreferencePrior(kind="palette", value={"bias": 0.3}, weight=1.0, signals=1)
    label, detail = describe(prior)
    assert label == "Warmer palette bias +0.3"
    assert "not yet applied" in detail  # +0.3 is below the apply threshold
    assert is_applied(prior) is False


def test_describe_leaning_when_weak() -> None:
    prior = PreferencePrior(kind="composition", value={"bias": 0.3}, weight=1.0, signals=1)
    label, _ = describe(prior)
    assert label == "Leaning toward wider, establishing framing"


def test_describe_zero_bias_is_zero_state() -> None:
    prior = PreferencePrior(kind="pacing", value={"bias": 0.0}, weight=0.0, signals=0)
    label, _ = describe(prior)
    assert label == "No preference learned yet"


def test_apply_threshold_constant_is_reachable_in_two_signals() -> None:
    # Two same-direction notes (0.6) clear the bar; one (0.3) does not.
    assert SIGNAL_STEP < APPLY_THRESHOLD <= 2 * SIGNAL_STEP


# --- new axes: lighting + energy (§8.6 enhancement) -------------------------- #


def test_infer_lighting_axis() -> None:
    assert infer_signals("make it darker and moodier") == [("lighting", -1)]
    assert infer_signals("brighter, more light please") == [("lighting", 1)]


def test_infer_energy_axis() -> None:
    assert infer_signals("make it more dramatic") == [("energy", 1)]
    assert infer_signals("calmer, more understated") == [("energy", -1)]


def test_lighting_and_energy_camera_free_but_describe() -> None:
    # Neither maps to a camera field; both still read as plain language.
    assert camera_overrides(_priors(lighting=-0.9, energy=0.9)) == {}
    dark = PreferencePrior(kind="lighting", value={"bias": -0.9}, weight=3.0, signals=1)
    assert describe(dark)[0] == "You prefer darker, moodier lighting"


# --- recency decay (§8.5 ethos) ---------------------------------------------- #


def test_decay_factor_halves_each_half_life() -> None:
    assert decay_factor(0) == 1.0
    assert abs(decay_factor(DECAY_HALF_LIFE_DAYS * 86_400) - 0.5) < 1e-6
    assert abs(decay_factor(2 * DECAY_HALF_LIFE_DAYS * 86_400) - 0.25) < 1e-6
    assert decay_factor(-100) == 1.0  # clock skew clamps to fresh


# --- visual grade (palette + lighting drive the off-gate ffmpeg grade) ------- #


def test_grade_for_applied_palette_and_lighting() -> None:
    grade = grade_for(_priors(palette=0.9, lighting=-0.9))
    assert grade == {"palette": "warm", "lighting": "dark"}
    # Weak priors don't grade; pacing/composition never grade (camera axes).
    assert grade_for(_priors(palette=0.3, composition=0.9)) == {}
