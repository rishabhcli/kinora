"""Base-interface tests + the cross-dialect budget/non-empty property test.

The property test is the load-bearing invariant for the whole layer: for ANY
ShotDescription and ANY (positive) budget, every registered dialect must produce a
prompt that is within budget and — when the description has any content — non-empty.
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from app.video.prompts.base import DialectSpec, NegativeStyle, RenderedPrompt
from app.video.prompts.canonical import (
    CameraAngle,
    CameraDirection,
    CameraMove,
    CameraSpeed,
    ShotDescription,
    ShotSize,
)
from app.video.prompts.registry import build_default_registry

_REGISTRY = build_default_registry()
_DIALECTS = [_REGISTRY.get(name) for name in _REGISTRY.names()]


# --------------------------------------------------------------------------- #
# Base interface
# --------------------------------------------------------------------------- #


def test_rendered_prompt_is_empty_property() -> None:
    assert RenderedPrompt(dialect="x", prompt="   ").is_empty
    assert not RenderedPrompt(dialect="x", prompt="hello").is_empty


def test_dialect_spec_and_rendered_prompt_are_frozen() -> None:
    import pytest
    from pydantic import ValidationError

    spec = DialectSpec(name="t")
    out = RenderedPrompt(dialect="t", prompt="p")
    with pytest.raises(ValidationError):
        spec.name = "u"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        out.prompt = "q"  # type: ignore[misc]


def test_negative_style_defaults() -> None:
    ns = NegativeStyle()
    assert ns.supported is False
    assert ns.budget == 512


def test_dialect_name_is_spec_name() -> None:
    for dialect in _DIALECTS:
        assert dialect.name == dialect.spec.name


def test_empty_description_renders_without_crashing() -> None:
    # An entirely empty shot: dialects may produce empty/near-empty text but must
    # not raise and must respect the budget.
    empty = ShotDescription()
    for dialect in _DIALECTS:
        out = dialect.render(empty)
        assert isinstance(out, RenderedPrompt)
        assert len(out.prompt) <= dialect.spec.prompt_budget


# --------------------------------------------------------------------------- #
# Property: non-empty + within budget for any ShotDescription / budget
# --------------------------------------------------------------------------- #

_text = st.text(
    # Exclude surrogates/unassigned so generated strings are valid, encodable text.
    alphabet=st.characters(codec="utf-8"),
    min_size=0,
    max_size=120,
)
_token_list = st.lists(_text, min_size=0, max_size=5)

_camera = st.builds(
    CameraDirection,
    shot_size=st.sampled_from(list(ShotSize)) | _text,
    move=st.sampled_from(list(CameraMove)) | _text,
    speed=st.sampled_from(list(CameraSpeed)) | _text,
    angle=st.none() | st.sampled_from(list(CameraAngle)) | _text,
)

_shot = st.builds(
    ShotDescription,
    subject=_text,
    action=_text,
    setting=_text,
    mood=_text,
    lighting=_text,
    camera=_camera,
    style_refs=_token_list,
    quality_tokens=_token_list,
    continuity_tags=_token_list,
    negative_cues=_token_list,
)


@settings(max_examples=250, deadline=None)
@given(shot=_shot, budget=st.integers(min_value=1, max_value=5000))
def test_every_dialect_within_budget_for_any_shot(shot: ShotDescription, budget: int) -> None:
    for dialect in _DIALECTS:
        out = dialect.render(shot, budget=budget)
        assert len(out.prompt) <= budget, f"{dialect.name} over budget {budget}: {out.prompt!r}"
        if dialect.spec.negative.supported and out.negative_prompt is not None:
            assert len(out.negative_prompt) <= dialect.spec.negative.budget


@settings(max_examples=200, deadline=None)
@given(shot=_shot)
def test_nonempty_when_subject_or_action_present(shot: ShotDescription) -> None:
    # If there is real positive content, every dialect produces a non-empty prompt
    # at its native budget.
    if not (shot.subject.strip() or shot.action.strip()):
        return
    for dialect in _DIALECTS:
        out = dialect.render(shot)
        assert out.prompt.strip(), f"{dialect.name} produced empty prompt for {shot!r}"


@settings(max_examples=150, deadline=None)
@given(shot=_shot)
def test_default_budget_render_respects_spec(shot: ShotDescription) -> None:
    for dialect in _DIALECTS:
        out = dialect.render(shot)
        assert out.dialect == dialect.name
        assert len(out.prompt) <= dialect.spec.prompt_budget
