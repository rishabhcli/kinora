"""Unit tests for VideoCapability + CapabilityQuery (pure, no infra, no network).

Covers: enum parity with WanMode, validator guards, case-insensitive membership,
duration window + discrete snapping, and the structured ``supports(query)``
predicate across every constraint axis.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.providers.types import WanMode
from app.video.abstraction.capability import (
    CapabilityQuery,
    ReferenceStyle,
    SubmitStyle,
    VideoCapability,
    VideoMode,
    normalize_aspect,
    normalize_resolution,
)


def _cap(**overrides: object) -> VideoCapability:
    base: dict[str, object] = {
        "provider_id": "p",
        "modes": frozenset({VideoMode.TEXT_TO_VIDEO, VideoMode.REFERENCE_TO_VIDEO}),
        "min_duration_s": 2.0,
        "max_duration_s": 10.0,
        "resolutions": ("480P", "720P"),
        "aspect_ratios": ("16:9", "9:16"),
        "fps_options": (16, 24),
        "supports_seed": True,
        "supports_negative_prompt": True,
        "supports_audio": False,
        "max_prompt_chars": 500,
        "submit_style": SubmitStyle.ASYNC_POLL,
    }
    base.update(overrides)
    return VideoCapability(**base)  # type: ignore[arg-type]


# -- enum parity ---------------------------------------------------------- #


def test_videomode_values_match_wanmode() -> None:
    """Canonical VideoMode mirrors WanMode value-for-value (lossless mapping)."""
    assert {m.value for m in VideoMode} == {m.value for m in WanMode}


# -- normalizers ---------------------------------------------------------- #


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(" 720p ", "720P"), ("1080P", "1080P"), ("480p", "480P")],
)
def test_normalize_resolution(raw: str, expected: str) -> None:
    assert normalize_resolution(raw) == expected


def test_normalize_aspect_strips() -> None:
    assert normalize_aspect("  16:9 ") == "16:9"


# -- validators ----------------------------------------------------------- #


def test_min_gt_max_duration_rejected() -> None:
    with pytest.raises(ValidationError):
        _cap(min_duration_s=10.0, max_duration_s=2.0)


def test_default_resolution_must_be_allowed() -> None:
    with pytest.raises(ValidationError):
        _cap(default_resolution="4K")


def test_default_resolution_case_insensitive_ok() -> None:
    cap = _cap(default_resolution="720p")  # lowercase, still in ("480P","720P")
    assert cap.default_resolution == "720p"


def test_default_fps_must_be_allowed() -> None:
    with pytest.raises(ValidationError):
        _cap(default_fps=60)


def test_discrete_duration_outside_window_rejected() -> None:
    with pytest.raises(ValidationError):
        _cap(discrete_durations_s=(2.0, 99.0))


def test_reference_style_none_forbids_reference_images() -> None:
    with pytest.raises(ValidationError):
        _cap(reference_style=ReferenceStyle.NONE, max_reference_images=2)


def test_empty_provider_id_rejected() -> None:
    with pytest.raises(ValidationError):
        _cap(provider_id="")


def test_capability_is_frozen() -> None:
    cap = _cap()
    with pytest.raises(ValidationError):
        cap.min_duration_s = 1.0  # type: ignore[misc]


# -- membership helpers --------------------------------------------------- #


def test_supports_resolution_case_insensitive() -> None:
    cap = _cap()
    assert cap.supports_resolution("720p")
    assert cap.supports_resolution(" 480P ")
    assert not cap.supports_resolution("1080P")


def test_supports_aspect_and_fps() -> None:
    cap = _cap()
    assert cap.supports_aspect_ratio("9:16")
    assert not cap.supports_aspect_ratio("4:3")
    assert cap.supports_fps(24)
    assert not cap.supports_fps(30)


# -- duration window + snapping ------------------------------------------ #


def test_allows_duration_window() -> None:
    cap = _cap()
    assert cap.allows_duration(2.0)
    assert cap.allows_duration(10.0)
    assert not cap.allows_duration(1.99)
    assert not cap.allows_duration(10.01)


def test_allows_duration_discrete() -> None:
    cap = _cap(discrete_durations_s=(2.0, 5.0, 10.0))
    assert cap.allows_duration(5.0)
    assert not cap.allows_duration(7.0)  # inside window but not a discrete step


def test_snap_duration_clamps() -> None:
    cap = _cap()
    assert cap.snap_duration(0.5) == 2.0
    assert cap.snap_duration(99.0) == 10.0
    assert cap.snap_duration(6.0) == 6.0  # continuous: unchanged


def test_snap_duration_to_nearest_discrete_tie_to_shorter() -> None:
    cap = _cap(discrete_durations_s=(2.0, 6.0, 10.0))
    assert cap.snap_duration(5.0) == 6.0
    assert cap.snap_duration(3.9) == 2.0
    # midpoint 4.0 between 2 and 6 → tie resolves to the shorter (conserve budget)
    assert cap.snap_duration(4.0) == 2.0


# -- supports(query) ------------------------------------------------------ #


def test_empty_query_matches() -> None:
    assert _cap().supports(CapabilityQuery())


def test_query_mode_constraint() -> None:
    cap = _cap()
    assert cap.supports(CapabilityQuery(mode=VideoMode.REFERENCE_TO_VIDEO))
    assert not cap.supports(CapabilityQuery(mode=VideoMode.IMAGE_TO_VIDEO))


def test_query_duration_constraint() -> None:
    cap = _cap()
    assert cap.supports(CapabilityQuery(duration_s=5.0))
    assert not cap.supports(CapabilityQuery(duration_s=20.0))


def test_query_resolution_and_fps() -> None:
    cap = _cap()
    assert cap.supports(CapabilityQuery(resolution="720p", fps=24))
    assert not cap.supports(CapabilityQuery(resolution="1080P"))
    assert not cap.supports(CapabilityQuery(fps=30))


def test_query_feature_flags() -> None:
    cap = _cap(supports_seed=False, supports_negative_prompt=False, supports_audio=False)
    assert not cap.supports(CapabilityQuery(needs_seed=True))
    assert not cap.supports(CapabilityQuery(needs_negative_prompt=True))
    assert not cap.supports(CapabilityQuery(needs_audio=True))
    # a query that doesn't require them still matches
    assert cap.supports(CapabilityQuery())


def test_query_async_flag() -> None:
    async_cap = _cap(submit_style=SubmitStyle.ASYNC_POLL)
    sync_cap = _cap(submit_style=SubmitStyle.SYNCHRONOUS)
    assert async_cap.supports(CapabilityQuery(needs_async=True))
    assert not async_cap.supports(CapabilityQuery(needs_async=False))
    assert sync_cap.supports(CapabilityQuery(needs_async=False))
    assert not sync_cap.supports(CapabilityQuery(needs_async=True))


def test_query_prompt_length_against_max() -> None:
    cap = _cap(max_prompt_chars=500)
    assert cap.supports(CapabilityQuery(prompt_length=500))
    assert not cap.supports(CapabilityQuery(prompt_length=501))


def test_query_prompt_length_unbounded_provider() -> None:
    cap = _cap(max_prompt_chars=None)
    assert cap.supports(CapabilityQuery(prompt_length=1_000_000))


def test_query_is_frozen_and_strict() -> None:
    with pytest.raises(ValidationError):
        CapabilityQuery(bogus=1)  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        CapabilityQuery(duration_s=0)  # gt=0
