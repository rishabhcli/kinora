"""Perceptual / aesthetic quality scoring — sharpness / exposure / contrast / color.

RGB grids are built directly (no PNG decode) so the pure proxies are exercised
deterministically. Grids are matrices of (r, g, b) triples in 0..1.
"""

from __future__ import annotations

from app.render.qa.aesthetic import (
    aesthetic_score,
    colorfulness_score,
    contrast_score,
    exposure_score,
    sharpness_score,
)

Rgb = list[list[tuple[float, float, float]]]


def _flat_gray(value: float, size: int = 8) -> Rgb:
    return [[(value, value, value)] * size for _ in range(size)]


def _gray_checker(size: int = 8) -> list[list[float]]:
    return [[1.0 if (r + c) % 2 == 0 else 0.0 for c in range(size)] for r in range(size)]


def _rgb_checker(size: int = 8) -> Rgb:
    return [
        [(1.0, 1.0, 1.0) if (r + c) % 2 == 0 else (0.0, 0.0, 0.0) for c in range(size)]
        for r in range(size)
    ]


def _vivid_checker(size: int = 8) -> Rgb:
    # Alternating saturated red / blue → high colorfulness.
    return [
        [(1.0, 0.0, 0.0) if (r + c) % 2 == 0 else (0.0, 0.0, 1.0) for c in range(size)]
        for r in range(size)
    ]


# --------------------------------------------------------------------------- #
# Sharpness
# --------------------------------------------------------------------------- #


def test_sharpness_low_on_blur() -> None:
    assert sharpness_score(_gray_checker_to_flat()) < 0.2


def _gray_checker_to_flat() -> list[list[float]]:
    return [[0.5] * 8 for _ in range(8)]  # a flat (blurry) frame


def test_sharpness_high_on_detail() -> None:
    assert sharpness_score(_gray_checker()) > 0.8


# --------------------------------------------------------------------------- #
# Exposure
# --------------------------------------------------------------------------- #


def test_exposure_good_on_midtones() -> None:
    assert exposure_score([[0.5] * 8 for _ in range(8)]) == 1.0


def test_exposure_ruined_on_clipping() -> None:
    # Half pure black, half pure white → everything clipped.
    grid = [[0.0] * 8 if r < 4 else [1.0] * 8 for r in range(8)]
    assert exposure_score(grid) < 0.2


# --------------------------------------------------------------------------- #
# Contrast
# --------------------------------------------------------------------------- #


def test_contrast_low_on_flat_frame() -> None:
    assert contrast_score([[0.5] * 8 for _ in range(8)]) == 0.0


def test_contrast_high_on_full_range() -> None:
    assert contrast_score(_gray_checker()) > 0.8


# --------------------------------------------------------------------------- #
# Colorfulness
# --------------------------------------------------------------------------- #


def test_colorfulness_low_on_gray() -> None:
    assert colorfulness_score(_flat_gray(0.5)) == 0.0


def test_colorfulness_high_on_vivid() -> None:
    assert colorfulness_score(_vivid_checker()) > 0.5


# --------------------------------------------------------------------------- #
# Blended report
# --------------------------------------------------------------------------- #


def test_aesthetic_high_on_sharp_colorful_frame() -> None:
    report = aesthetic_score([_vivid_checker()])
    assert report.aesthetic > 0.7
    assert report.n_frames == 1


def test_aesthetic_low_on_flat_gray_frame() -> None:
    report = aesthetic_score([_flat_gray(0.5)])
    # Flat gray: no sharpness, no contrast, no color → ugly.
    assert report.aesthetic < 0.3


def test_aesthetic_empty_is_neutral() -> None:
    report = aesthetic_score([])
    assert report.aesthetic == 1.0
    assert report.n_frames == 0
