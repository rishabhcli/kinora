"""Pure aspect-fit geometry: the letterbox / pillarbox / focal-point-crop math.

Given a source W×H and a target W×H, compute the *exact* scaled size and the
pad offsets (PAD) or the crop window (CROP) — as plain integers, with no ffmpeg
involved — so the geometry is fully unit-testable in isolation and the plan layer
merely formats the numbers into a filter string.

All outputs keep even dimensions (yuv420p chroma subsampling needs them) and clamp
crop windows inside the scaled frame, so a focal-point hint near an edge can never
produce an out-of-bounds crop.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from .targets import FocalPoint


def _round_even(value: float) -> int:
    """Round to the nearest non-negative even integer (chroma-safe)."""
    n = int(round(value))
    if n < 0:
        n = 0
    return n - (n % 2)


def _at_least_even(value: float, floor: int = 2) -> int:
    return max(floor, _round_even(value))


class PadFit(BaseModel):
    """A letterbox/pillarbox plan: scale to ``(scaled_w, scaled_h)`` then pad."""

    model_config = ConfigDict(extra="forbid")

    scaled_w: int
    scaled_h: int
    target_w: int
    target_h: int
    pad_x: int
    pad_y: int

    @property
    def needs_pad(self) -> bool:
        return self.pad_x > 0 or self.pad_y > 0


class CropFit(BaseModel):
    """A fill-and-crop plan: scale to cover the target, then crop a window."""

    model_config = ConfigDict(extra="forbid")

    scaled_w: int
    scaled_h: int
    target_w: int
    target_h: int
    crop_x: int
    crop_y: int

    @property
    def needs_crop(self) -> bool:
        return self.scaled_w != self.target_w or self.scaled_h != self.target_h


def plan_pad_fit(
    src: tuple[int, int],
    target: tuple[int, int],
) -> PadFit:
    """Scale ``src`` to fit *inside* ``target`` (preserving aspect), centre-padded.

    The classic letterbox/pillarbox: the larger source dimension touches the
    frame edge, the other is centred with equal bars. Content is never cropped.
    """
    src_w, src_h = src
    tgt_w, tgt_h = target
    if src_w <= 0 or src_h <= 0:
        # Degenerate source: fall back to filling the whole frame.
        return PadFit(
            scaled_w=tgt_w, scaled_h=tgt_h, target_w=tgt_w, target_h=tgt_h, pad_x=0, pad_y=0
        )
    scale = min(tgt_w / src_w, tgt_h / src_h)
    scaled_w = min(tgt_w, _at_least_even(src_w * scale))
    scaled_h = min(tgt_h, _at_least_even(src_h * scale))
    pad_x = _round_even((tgt_w - scaled_w) / 2)
    pad_y = _round_even((tgt_h - scaled_h) / 2)
    return PadFit(
        scaled_w=scaled_w,
        scaled_h=scaled_h,
        target_w=tgt_w,
        target_h=tgt_h,
        pad_x=pad_x,
        pad_y=pad_y,
    )


def plan_crop_fit(
    src: tuple[int, int],
    target: tuple[int, int],
    *,
    focal: FocalPoint | None = None,
) -> CropFit:
    """Scale ``src`` to *cover* ``target`` then crop, biased by a focal point.

    The source is scaled up until both target dimensions are covered (the smaller
    overflow axis spills past the frame), then a target-sized window is cropped.
    The window is centred on ``focal`` (default centre) and clamped fully inside
    the scaled frame so an edge-hugging focal point can never crop out of bounds.
    """
    src_w, src_h = src
    tgt_w, tgt_h = target
    point = focal or FocalPoint.center()
    if src_w <= 0 or src_h <= 0:
        return CropFit(
            scaled_w=tgt_w, scaled_h=tgt_h, target_w=tgt_w, target_h=tgt_h, crop_x=0, crop_y=0
        )
    scale = max(tgt_w / src_w, tgt_h / src_h)
    scaled_w = max(tgt_w, _at_least_even(src_w * scale))
    scaled_h = max(tgt_h, _at_least_even(src_h * scale))
    # Centre the crop window on the focal point, then clamp inside the frame.
    raw_x = point.x * scaled_w - tgt_w / 2
    raw_y = point.y * scaled_h - tgt_h / 2
    crop_x = _clamp_even(raw_x, 0, scaled_w - tgt_w)
    crop_y = _clamp_even(raw_y, 0, scaled_h - tgt_h)
    return CropFit(
        scaled_w=scaled_w,
        scaled_h=scaled_h,
        target_w=tgt_w,
        target_h=tgt_h,
        crop_x=crop_x,
        crop_y=crop_y,
    )


def _clamp_even(value: float, low: int, high: int) -> int:
    """Round to even and clamp into ``[low, high]`` (``high`` may be 0)."""
    n = _round_even(value)
    if n < low:
        n = low
    if high >= low and n > high:
        n = high - (high % 2)
    return max(low, n)


__all__ = [
    "CropFit",
    "PadFit",
    "plan_crop_fit",
    "plan_pad_fit",
]
