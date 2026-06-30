"""Golden-output tests for the frontier dialects (Runway/Pika/Kling/Luma/Veo/Sora/generic).

Each asserts the *exact* prompt + negative-prompt a representative ShotDescription
renders to, pinning each model's idiomatic phrasing, camera grammar, and negative
handling (native channel vs. folded clause).
"""

from __future__ import annotations

from app.video.prompts.canonical import (
    CameraAngle,
    CameraDirection,
    CameraMove,
    CameraSpeed,
    ShotDescription,
    ShotSize,
)
from app.video.prompts.dialects.generic import GenericDialect
from app.video.prompts.dialects.kling import KlingDialect
from app.video.prompts.dialects.luma import LumaDialect
from app.video.prompts.dialects.pika import PikaDialect
from app.video.prompts.dialects.runway import RunwayDialect
from app.video.prompts.dialects.sora import SoraDialect
from app.video.prompts.dialects.veo import VeoDialect


def _sample() -> ShotDescription:
    return ShotDescription(
        subject="a lone knight",
        action="rides across a misted moor",
        setting="a grey dawn moor",
        mood="ominous",
        lighting="cold rim light",
        camera=CameraDirection(
            shot_size=ShotSize.WIDE,
            move=CameraMove.PUSH_IN,
            speed=CameraSpeed.SLOW,
            angle=CameraAngle.LOW_ANGLE,
        ),
        style_refs=["anamorphic", "Roger Deakins"],
        quality_tokens=["cinematic", "film grain"],
        negative_cues=["blurry", "extra fingers", "watermark"],
    )


def test_runway_golden() -> None:
    out = RunwayDialect().render(_sample())
    assert out.dialect == "runway"
    assert out.negative_prompt is None  # no native channel
    assert out.prompt == (
        "a lone knight rides across a misted moor. a grey dawn moor. "
        "camera: wide shot, slow dolly in, low angle. cold rim light. ominous. "
        "anamorphic, Roger Deakins. cinematic, film grain. "
        "no blurry, extra fingers, watermark"
    )


def test_pika_golden_short_with_camera_and_neg_flags() -> None:
    out = PikaDialect().render(_sample())
    assert out.dialect == "pika"
    assert out.negative_prompt is None
    assert "-camera zoom in" in out.prompt
    assert "-neg blurry extra fingers watermark" in out.prompt
    assert len(out.prompt) <= PikaDialect().spec.prompt_budget


def test_pika_keeps_flags_when_text_is_truncated() -> None:
    # A huge action forces the scene text to be dropped/truncated, but the
    # -camera / -neg control flags survive (reserved out of the budget first).
    shot = _sample().model_copy(update={"action": "x " * 400})
    out = PikaDialect().render(shot)
    assert "-camera zoom in" in out.prompt
    assert "-neg" in out.prompt
    assert len(out.prompt) <= PikaDialect().spec.prompt_budget


def test_kling_golden_with_native_negative() -> None:
    out = KlingDialect().render(_sample())
    assert out.dialect == "kling"
    assert out.negative_prompt == "blurry, extra fingers, watermark"
    assert out.prompt == (
        "a lone knight rides across a misted moor. a grey dawn moor. "
        "wide shot, camera pushes in slowly, low angle. cold rim light. ominous. "
        "anamorphic, Roger Deakins. cinematic, film grain"
    )


def test_luma_golden_flowing_prose_folds_negative() -> None:
    out = LumaDialect().render(_sample())
    assert out.dialect == "luma"
    assert out.negative_prompt is None
    assert out.prompt == (
        "a wide establishing frame: a lone knight rides across a misted moor. "
        "set in a grey dawn moor. the camera glides in slowly. shot from a low angle. "
        "cold rim light. the mood is ominous. anamorphic, Roger Deakins. "
        "cinematic, film grain. without blurry, extra fingers, watermark"
    )


def test_veo_golden_long_prose_native_negative() -> None:
    out = VeoDialect().render(_sample())
    assert out.dialect == "veo"
    assert out.negative_prompt == "blurry, extra fingers, watermark"
    assert out.prompt == (
        "a wide establishing shot of a lone knight rides across a misted moor. "
        "the scene is set in a grey dawn moor. "
        "the camera performs a slow dolly push-in, slow and deliberate, from a low angle. "
        "lit with cold rim light. the mood is ominous. "
        "in the style of anamorphic, Roger Deakins. cinematic, film grain"
    )


def test_sora_golden_long_prose_folds_negative() -> None:
    out = SoraDialect().render(_sample())
    assert out.dialect == "sora"
    assert out.negative_prompt is None
    assert out.prompt == (
        "a wide establishing shot of a lone knight rides across a misted moor. "
        "set in a grey dawn moor. "
        "captured with a slow dolly push-in, slow and deliberate, from a low angle. "
        "lit by cold rim light. evoking a ominous mood. "
        "rendered in the style of anamorphic, Roger Deakins. cinematic, film grain. "
        "avoiding blurry, extra fingers, watermark"
    )


def test_generic_golden_neutral_baseline() -> None:
    out = GenericDialect().render(_sample())
    assert out.dialect == "generic"
    assert out.negative_prompt is None
    assert out.prompt == (
        "a lone knight rides across a misted moor. a grey dawn moor. "
        "wide shot, slow dolly in, low angle. cold rim light. ominous. "
        "anamorphic, Roger Deakins. cinematic, film grain. "
        "avoid blurry, extra fingers, watermark"
    )


def test_dialects_distinguish_zoom_directions() -> None:
    # The canonical ZOOM_IN/ZOOM_OUT (and undirected ZOOM) phrase differently.
    base = ShotDescription(subject="a face", action="reacts")
    zin = VeoDialect().render(
        base.model_copy(update={"camera": CameraDirection(move=CameraMove.ZOOM_IN)})
    )
    zout = VeoDialect().render(
        base.model_copy(update={"camera": CameraDirection(move=CameraMove.ZOOM_OUT)})
    )
    assert "zoom-in" in zin.prompt
    assert "zoom-out" in zout.prompt


def test_freeform_camera_passes_through_in_frontier_dialect() -> None:
    shot = ShotDescription(
        subject="a dancer",
        action="spins",
        camera=CameraDirection(move="whip-pan into a snap zoom", shot_size="over-the-shoulder"),
    )
    out = RunwayDialect().render(shot)
    assert "whip-pan into a snap zoom" in out.prompt
    assert "over-the-shoulder" in out.prompt
