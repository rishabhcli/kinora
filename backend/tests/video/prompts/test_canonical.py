"""Tests for the canonical, model-agnostic ShotDescription + camera coercion."""

from __future__ import annotations

import pytest

from app.video.prompts.canonical import (
    CameraDirection,
    CameraMove,
    CameraSpeed,
    RenderIntent,
    ShotDescription,
    ShotSize,
    coerce_move,
    coerce_shot_size,
    coerce_speed,
)


def test_shot_description_defaults_are_neutral() -> None:
    shot = ShotDescription()
    assert shot.subject == "" and shot.action == ""
    assert isinstance(shot.camera, CameraDirection)
    assert shot.camera.move is CameraMove.STATIC
    assert shot.camera.shot_size is ShotSize.MEDIUM
    assert shot.camera.speed is CameraSpeed.MEDIUM
    assert shot.camera.angle is None
    assert shot.intent is RenderIntent.TEXT_TO_VIDEO
    assert shot.negative_cues == []


def test_shot_description_forbids_unknown_fields() -> None:
    with pytest.raises(ValueError):
        ShotDescription(unknown_field="x")  # type: ignore[call-arg]


@pytest.mark.parametrize(
    ("alias", "expected"),
    [
        ("push", CameraMove.PUSH_IN),
        ("push_in", CameraMove.PUSH_IN),
        ("dolly_in", CameraMove.PUSH_IN),
        ("Pull-Out", CameraMove.PULL_OUT),
        ("dolly_out", CameraMove.PULL_OUT),
        ("TRACK", CameraMove.TRACK),
        ("tracking", CameraMove.TRACK),
        ("orbit", CameraMove.ORBIT),
        ("arc", CameraMove.ORBIT),
        ("crane", CameraMove.CRANE_UP),
        ("zoom", CameraMove.ZOOM),
        ("zoom_in", CameraMove.ZOOM_IN),
        ("locked", CameraMove.STATIC),
        ("  static ", CameraMove.STATIC),
    ],
)
def test_coerce_move_maps_aliases(alias: str, expected: CameraMove) -> None:
    assert coerce_move(alias) is expected


def test_coerce_move_passes_unknown_through_cleaned() -> None:
    # An unknown move survives as cleaned free text (no enum forced).
    assert coerce_move("  whip-pan into snap zoom ") == "whip-pan into snap zoom"


@pytest.mark.parametrize(
    ("alias", "expected"),
    [
        ("close", ShotSize.CLOSE_UP),
        ("close-up", ShotSize.CLOSE_UP),
        ("establishing", ShotSize.WIDE),
        ("extreme_close", ShotSize.EXTREME_CLOSE_UP),
        ("Cowboy", ShotSize.COWBOY),
    ],
)
def test_coerce_shot_size_maps_aliases(alias: str, expected: ShotSize) -> None:
    assert coerce_shot_size(alias) is expected


def test_coerce_shot_size_passes_unknown_through() -> None:
    assert coerce_shot_size("over-the-shoulder") == "over-the-shoulder"


@pytest.mark.parametrize(
    ("alias", "expected"),
    [("slow", CameraSpeed.SLOW), ("steady", CameraSpeed.MEDIUM), ("fast", CameraSpeed.FAST)],
)
def test_coerce_speed_maps_aliases(alias: str, expected: CameraSpeed) -> None:
    assert coerce_speed(alias) is expected


def test_coerce_speed_passes_unknown_through() -> None:
    assert coerce_speed("glacial") == "glacial"


def test_camera_direction_from_agent_camera_coerces_each_field() -> None:
    cam = CameraDirection.from_agent_camera(
        shot_size="close", move="push", speed="slow", angle="low angle"
    )
    assert cam.shot_size is ShotSize.CLOSE_UP
    assert cam.move is CameraMove.PUSH_IN
    assert cam.speed is CameraSpeed.SLOW
    assert cam.angle == "low angle"


def test_camera_direction_from_agent_camera_blank_angle_is_none() -> None:
    cam = CameraDirection.from_agent_camera(
        shot_size="medium", move="static", speed="medium", angle="  "
    )
    assert cam.angle is None
