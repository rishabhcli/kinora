"""Unit tests for WEBVTT authoring (pure, no ffmpeg)."""

from __future__ import annotations

import pytest

from app.media.vtt import Chapter, chapters_vtt, format_timestamp, sprite_vtt


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (0.0, "00:00:00.000"),
        (1.5, "00:00:01.500"),
        (61.25, "00:01:01.250"),
        (3661.001, "01:01:01.001"),
        (-5.0, "00:00:00.000"),
    ],
)
def test_format_timestamp(seconds: float, expected: str) -> None:
    assert format_timestamp(seconds) == expected


def test_sprite_vtt_header_and_cues() -> None:
    vtt = sprite_vtt(
        sprite_url="https://cdn/x/sprite.png",
        columns=2,
        rows=2,
        tile_width=160,
        tile_height=90,
        tile_count=4,
        interval_s=2.5,
    )
    assert vtt.startswith("WEBVTT\n")
    # first tile at origin, second tile to the right, third on next row
    assert "00:00:00.000 --> 00:00:02.500" in vtt
    assert "sprite.png#xywh=0,0,160,90" in vtt
    assert "sprite.png#xywh=160,0,160,90" in vtt
    assert "sprite.png#xywh=0,90,160,90" in vtt
    # four cues
    assert vtt.count("#xywh=") == 4
    assert vtt.endswith("\n")


def test_sprite_vtt_stops_at_grid_capacity() -> None:
    # count exceeds the grid → never index past the sheet
    vtt = sprite_vtt(
        sprite_url="s.png",
        columns=2,
        rows=1,
        tile_width=100,
        tile_height=100,
        tile_count=5,
        interval_s=1.0,
    )
    assert vtt.count("#xywh=") == 2


def test_sprite_vtt_rejects_bad_grid() -> None:
    with pytest.raises(ValueError):
        sprite_vtt(
            sprite_url="s",
            columns=0,
            rows=1,
            tile_width=1,
            tile_height=1,
            tile_count=1,
            interval_s=1.0,
        )


def test_chapters_vtt_sorted_and_numbered() -> None:
    chapters = [
        Chapter(10.0, 20.0, "Scene B"),
        Chapter(0.0, 10.0, "Scene A"),
    ]
    vtt = chapters_vtt(chapters)
    assert vtt.startswith("WEBVTT\n")
    a_pos = vtt.index("Scene A")
    b_pos = vtt.index("Scene B")
    assert a_pos < b_pos  # sorted by start
    assert "1\n00:00:00.000 --> 00:00:10.000\nScene A" in vtt
    assert "2\n00:00:10.000 --> 00:00:20.000\nScene B" in vtt


def test_chapters_vtt_empty() -> None:
    assert chapters_vtt([]).strip() == "WEBVTT"
