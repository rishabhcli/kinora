"""Golden + faithfulness tests for the Wan dialect.

The Wan dialect MUST reproduce ``app.agents.generator.compose_wan_prompt``
byte-for-byte for every camera the generator can phrase, so lifting the existing
Wan render path onto the canonical ShotDescription changes no output. These tests
pin that contract against the real generator.
"""

from __future__ import annotations

import pytest

from app.agents.contracts import Camera
from app.agents.generator import (
    _MOVE_PHRASES,
    _SHOT_PHRASES,
    _SPEED_PHRASES,
    compose_wan_prompt,
)
from app.video.prompts.canonical import (
    CameraDirection,
    coerce_move,
    coerce_shot_size,
    coerce_speed,
)
from app.video.prompts.dialects.wan import (
    WAN_CINEMATIC_FINISH,
    WanDialect,
    compose_like_generator,
    shot_from_wan,
    wan_camera_phrase,
)

_PROMPT = "A lone knight rides across the misted moor"


def _coerced(shot_size: str, speed: str, move: str) -> CameraDirection:
    return CameraDirection(
        shot_size=coerce_shot_size(shot_size),
        speed=coerce_speed(speed),
        move=coerce_move(move),
    )


@pytest.mark.parametrize("shot_size", list(_SHOT_PHRASES))
@pytest.mark.parametrize("move", list(_MOVE_PHRASES))
def test_wan_matches_generator_for_every_known_camera(shot_size: str, move: str) -> None:
    """Across every generator shot/move alias (× medium speed), output is identical."""
    cam_kwargs = {"shot_size": shot_size, "move": move, "speed": "medium"}
    gen = compose_wan_prompt(_PROMPT, Camera(**cam_kwargs))
    mine = compose_like_generator(_PROMPT, _coerced(shot_size, "medium", move))
    assert mine == gen


@pytest.mark.parametrize("speed", list(_SPEED_PHRASES))
def test_wan_matches_generator_for_every_speed(speed: str) -> None:
    gen = compose_wan_prompt(_PROMPT, Camera(shot_size="wide", move="track", speed=speed))
    mine = compose_like_generator(_PROMPT, _coerced("wide", speed, "track"))
    assert mine == gen


def test_wan_matches_generator_for_freeform_camera_values() -> None:
    # Unknown camera tokens pass through identically in both paths.
    cam = Camera(shot_size="dramatic", move="whip-pan", speed="glacial")
    gen = compose_wan_prompt(_PROMPT, cam)
    mine = compose_like_generator(
        _PROMPT,
        CameraDirection(
            shot_size=coerce_shot_size("dramatic"),
            speed=coerce_speed("glacial"),
            move=coerce_move("whip-pan"),
        ),
    )
    assert mine == gen


def test_wan_matches_generator_for_empty_prompt() -> None:
    gen = compose_wan_prompt("", Camera())
    mine = compose_like_generator("", CameraDirection())
    assert mine == gen
    assert mine.startswith("Camera:")


def test_wan_camera_phrase_is_generator_phrase() -> None:
    cam = _coerced("close", "slow", "push")
    assert wan_camera_phrase(cam) == "intimate close-up, slow and deliberate slow dolly push-in"


def test_wan_cinematic_finish_matches_generator_constant() -> None:
    from app.agents import generator

    assert WAN_CINEMATIC_FINISH == generator._CINEMATIC_FINISH


def test_wan_dialect_render_reproduces_generator_via_shot_from_wan() -> None:
    cam = _coerced("close", "slow", "push")
    shot = shot_from_wan(prompt=_PROMPT, camera=cam, negative_prompt="blurry, extra fingers")
    out = WanDialect().render(shot)
    expected = compose_wan_prompt(_PROMPT, Camera(shot_size="close", move="push", speed="slow"))
    assert out.prompt == expected
    assert out.dialect == "wan"


def test_wan_negative_prompt_is_native_channel_deduped() -> None:
    shot = shot_from_wan(
        prompt=_PROMPT,
        camera=CameraDirection(),
        negative_prompt="blurry, Blurry, extra fingers",
    )
    out = WanDialect().render(shot)
    # Dedupe is case-insensitive, first casing kept; native negative channel used.
    assert out.negative_prompt == "blurry, extra fingers"


def test_wan_no_negatives_yields_none() -> None:
    shot = shot_from_wan(prompt=_PROMPT, camera=CameraDirection())
    out = WanDialect().render(shot)
    assert out.negative_prompt is None


def test_wan_golden_full_description() -> None:
    """A from-scratch canonical description renders the expected Wan string."""
    from app.video.prompts.canonical import CameraMove, CameraSpeed, ShotSize

    shot = shot_from_wan(
        prompt="A lone knight rides across the misted moor",
        camera=CameraDirection(
            shot_size=ShotSize.WIDE, move=CameraMove.PUSH_IN, speed=CameraSpeed.SLOW
        ),
    )
    out = WanDialect().render(shot)
    assert out.prompt == (
        "A lone knight rides across the misted moor. "
        "Camera: wide establishing shot, slow and deliberate slow dolly push-in. "
        "cinematic composition, volumetric lighting, shallow depth of field, "
        "fluid lifelike motion, atmospheric detail, film grain"
    )
