"""Unit tests for narration prosody planning: deterministic stress heuristics,
break classification, the instruct-style summary, and 1:1 alignment with words.
Pure — no model call, no spend."""

from __future__ import annotations

from app.providers.prosody import (
    BreakStrength,
    ProsodyPlan,
    apply_to_words,
    bare_word,
    plan_prosody,
)


def test_bare_word_strips_markup_and_punctuation() -> None:
    assert bare_word("*Storm*!") == "storm"
    assert bare_word('"Never,"') == "never"
    assert bare_word("__rage__") == "rage"


def test_plan_is_one_to_one_with_tokens() -> None:
    plan = plan_prosody("She stood at the door.")
    assert len(plan.marks) == 5
    assert [m.text for m in plan.marks] == ["She", "stood", "at", "the", "door."]


def test_function_words_are_unstressed() -> None:
    plan = plan_prosody("the cat and the dog")
    by_word = {bare_word(m.text): m.stress for m in plan.marks}
    assert by_word["the"] < 0.3
    assert by_word["and"] < 0.3
    assert by_word["cat"] > by_word["the"]  # content word beats function word


def test_caps_and_exclaim_raise_stress() -> None:
    plan = plan_prosody("STOP now!")
    stop = next(m for m in plan.marks if bare_word(m.text) == "stop")
    now = next(m for m in plan.marks if bare_word(m.text) == "now")
    assert stop.stress > 0.6  # ALL CAPS
    assert now.stress > 0.5  # trailing "!"


def test_markup_emphasis_raises_stress() -> None:
    plan = plan_prosody("a *quiet* word")
    quiet = next(m for m in plan.marks if bare_word(m.text) == "quiet")
    plain = plan_prosody("a quiet word").marks[1]
    assert quiet.stress > plain.stress


def test_break_classification() -> None:
    plan = plan_prosody("Wait, listen. Now go!")
    breaks = {bare_word(m.text): m.break_after for m in plan.marks}
    assert breaks["wait"] is BreakStrength.WEAK  # comma
    assert breaks["listen"] is BreakStrength.STRONG  # period
    assert breaks["go"] is BreakStrength.STRONG  # exclamation
    assert breaks["now"] is BreakStrength.NONE


def test_break_seconds_increase_with_strength() -> None:
    plan = plan_prosody("Wait, listen.")
    comma = next(m for m in plan.marks if m.break_after is BreakStrength.WEAK)
    period = next(m for m in plan.marks if m.break_after is BreakStrength.STRONG)
    assert period.break_s > comma.break_s > 0


def test_style_instruction_mentions_emphasis_and_pauses() -> None:
    plan = plan_prosody("The STORM raged. It never stopped!")
    instr = plan.style_instruction
    assert "storm" in instr.lower()
    assert "pausing at sentence ends" in instr
    assert instr.endswith(".")


def test_empty_text_yields_neutral_plan() -> None:
    plan = plan_prosody("")
    assert plan.marks == ()
    assert "calm" in plan.style_instruction.lower()


def test_stressed_words_accessor() -> None:
    plan = plan_prosody("a *mighty* HERO appeared")
    stressed = plan.stressed_words
    assert "mighty" in stressed
    assert "hero" in stressed
    assert "a" not in stressed


def test_apply_to_words_positional() -> None:
    plan = plan_prosody("STOP now")
    # Three "words" but only two tokens planned → third gets neutral 0.4.
    stresses = apply_to_words([object(), object(), object()], plan)
    assert len(stresses) == 3
    assert stresses[0] > 0.6  # STOP
    assert stresses[2] == 0.4  # past the plan → neutral


def test_provider_exposes_plan_prosody() -> None:
    from app.core.config import Settings
    from app.providers.base import ProviderClient
    from app.providers.tts import TtsProvider

    provider = TtsProvider(ProviderClient(Settings(dashscope_api_key="test")))
    plan = provider.plan_prosody("Hello there!")
    assert isinstance(plan, ProsodyPlan)
    assert plan.marks
