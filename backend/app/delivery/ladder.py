"""Rendition ladders — the set of bitrate/resolution rungs an ABR stream offers.

A *rendition* is one encoding of the film at a chosen resolution + bitrate; a
*ladder* is the ordered set the player chooses between as bandwidth changes.
Kinora's film is **vertical** (720x1280 — short-drama / phone-native reel, see
``app.render.degrade.FILM_SIZE``), so the default ladder steps *down* the short
edge — 720 → 540 → 360 → 240 — never up past the mastered geometry (upscaling a
720-wide master to "1080" would be a lie and waste bytes).

This module is **pure** — every function here is deterministic and free of
ffmpeg / network / DB — so ladder selection and the derived encode parameters
are fully unit-testable. The packaging plan layer (:mod:`app.delivery.segmenter`)
turns a chosen :class:`Rendition` into ffmpeg arguments; nothing here shells out.

Why a ladder *clamped to the master*: each per-shot clip is mastered once at the
film geometry, then transcoded down. Offering a rung *above* the master forces
an upscale that adds no detail, breaks seamless ABR switching (the decoder sees
a resolution jump with no new information), and inflates the budget — so
:func:`build_ladder` filters rungs taller than the source and always keeps at
least the source rung.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.delivery.errors import LadderError

#: A standard H.264 high-profile level string for vertical sub-1080 renditions.
#: (All Kinora renditions sit comfortably under level 4.0 — 720x1280@30 is well
#: within 4.0's macroblock/bitrate ceilings.)
_H264_PROFILE = "high"


class Rendition(BaseModel):
    """One rung of an ABR ladder: a resolution + a target/peak video bitrate.

    Geometry is stored as ``width`` x ``height`` (vertical: ``height`` is the
    long edge). ``video_bitrate_kbps`` is the average target the encoder aims
    for; ``max_bitrate_kbps`` is the VBV peak (defaults to 1.5x target) and
    ``audio_bitrate_kbps`` the AAC bitrate muxed into the rendition.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1)
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    video_bitrate_kbps: int = Field(gt=0)
    max_bitrate_kbps: int = Field(gt=0)
    audio_bitrate_kbps: int = Field(default=128, gt=0)
    fps: int = Field(default=30, gt=0)
    codec: str = "h264"
    profile: str = _H264_PROFILE

    @field_validator("name")
    @classmethod
    def _strip_name(cls, value: str) -> str:
        return value.strip()

    @property
    def short_edge(self) -> int:
        """The shorter dimension — what the rung is conventionally named by."""
        return min(self.width, self.height)

    @property
    def total_bitrate_kbps(self) -> int:
        """Average video + audio bitrate — the figure HLS ``BANDWIDTH`` reports."""
        return self.video_bitrate_kbps + self.audio_bitrate_kbps

    @property
    def peak_bandwidth_bps(self) -> int:
        """Peak (video VBV + audio) bandwidth in bits/s — HLS ``BANDWIDTH``."""
        return (self.max_bitrate_kbps + self.audio_bitrate_kbps) * 1000

    @property
    def average_bandwidth_bps(self) -> int:
        """Average bandwidth in bits/s — HLS ``AVERAGE-BANDWIDTH``."""
        return self.total_bitrate_kbps * 1000

    @property
    def resolution(self) -> str:
        """``WxH`` as manifests print it (HLS ``RESOLUTION``, DASH ``width/height``)."""
        return f"{self.width}x{self.height}"

    @property
    def rfc6381_codecs(self) -> str:
        """The RFC 6381 ``CODECS`` string an HLS/DASH manifest advertises.

        H.264 high@4.0 = ``avc1.640028``; AAC-LC = ``mp4a.40.2``. Renditions are
        normalized to this exact codec string so a player can switch between
        rungs without re-probing — the whole point of provider normalization.
        """
        return "avc1.640028,mp4a.40.2"


def scale_to_short_edge(width: int, height: int, target_short_edge: int) -> tuple[int, int]:
    """Scale a ``width``x``height`` frame so its short edge is ``target_short_edge``.

    Preserves aspect ratio and rounds **both** dimensions to even numbers (H.264
    chroma subsampling requires even width/height). Used to derive every rung's
    geometry from the mastered film geometry so the aspect never drifts.
    """
    if width <= 0 or height <= 0 or target_short_edge <= 0:
        raise LadderError("scale dimensions must be positive")
    short = min(width, height)
    scale = target_short_edge / short
    new_w = _even(round(width * scale))
    new_h = _even(round(height * scale))
    return new_w, new_h


def _even(value: int) -> int:
    """Round *up* to the nearest even integer >= 2 (H.264 needs even dims)."""
    value = max(2, value)
    return value if value % 2 == 0 else value + 1


#: The default ABR rungs, named by short edge with a sensible H.264 bitrate for
#: a vertical phone-native reel. Bitrates are conservative (motion in a slow
#: cinematic shot is low) and monotonic with resolution so the ladder is valid.
_DEFAULT_RUNGS: tuple[tuple[str, int, int, int], ...] = (
    # (name, short_edge, video_kbps, audio_kbps)
    ("720p", 720, 2800, 128),
    ("540p", 540, 1600, 128),
    ("360p", 360, 900, 96),
    ("240p", 240, 450, 64),
)


def build_ladder(
    *,
    source_width: int,
    source_height: int,
    fps: int = 30,
    rungs: Sequence[tuple[str, int, int, int]] | None = None,
    max_bitrate_ratio: float = 1.5,
) -> list[Rendition]:
    """Build an ABR ladder from the source geometry, clamped to the master.

    Each rung is scaled to its short edge from ``source_width``x``source_height``
    (preserving aspect). Rungs whose short edge exceeds the source short edge are
    dropped (no upscaling); a rung equal to the source is kept verbatim. The
    result is sorted **descending** by bandwidth (the master playlist convention
    is highest-first is not required, but a stable order makes manifests stable)
    and de-duplicated by resolution. At least the highest viable rung is always
    returned, so a tiny source still yields a one-rung ladder.

    Raises:
        LadderError: if the source geometry is non-positive or no rung survives.
    """
    if source_width <= 0 or source_height <= 0:
        raise LadderError("source geometry must be positive")
    if fps <= 0:
        raise LadderError("fps must be positive")
    rung_defs = rungs if rungs is not None else _DEFAULT_RUNGS
    source_short = min(source_width, source_height)
    out: list[Rendition] = []
    seen: set[str] = set()
    for name, short_edge, vkbps, akbps in rung_defs:
        if short_edge > source_short:
            continue  # never upscale past the master
        eff_short = min(short_edge, source_short)
        w, h = scale_to_short_edge(source_width, source_height, eff_short)
        resolution = f"{w}x{h}"
        if resolution in seen:
            continue
        seen.add(resolution)
        out.append(
            Rendition(
                name=name,
                width=w,
                height=h,
                video_bitrate_kbps=vkbps,
                max_bitrate_kbps=max(vkbps + 1, round(vkbps * max_bitrate_ratio)),
                audio_bitrate_kbps=akbps,
                fps=fps,
            )
        )
    if not out:
        # Source smaller than the smallest rung: emit a single source-sized rung.
        w, h = scale_to_short_edge(source_width, source_height, source_short)
        top = rung_defs[0] if rung_defs else ("source", source_short, 900, 96)
        out.append(
            Rendition(
                name="source",
                width=w,
                height=h,
                video_bitrate_kbps=top[2],
                max_bitrate_kbps=max(top[2] + 1, round(top[2] * max_bitrate_ratio)),
                audio_bitrate_kbps=top[3],
                fps=fps,
            )
        )
    return sort_ladder(out)


def sort_ladder(renditions: Iterable[Rendition]) -> list[Rendition]:
    """Return the renditions sorted by descending peak bandwidth (stable, by name).

    A canonical, deterministic order makes generated manifests byte-stable across
    runs (important for caching + golden tests).
    """
    return sorted(renditions, key=lambda r: (-r.peak_bandwidth_bps, r.name))


def select_rendition(
    ladder: Sequence[Rendition], *, available_bps: int, headroom: float = 0.85
) -> Rendition:
    """Pick the richest rung that fits within ``headroom`` of the available bandwidth.

    Mirrors a player's ABR heuristic: choose the highest-bitrate rendition whose
    *peak* bandwidth fits under ``available_bps * headroom`` (the headroom keeps
    a buffer for variance). If nothing fits — the link is below even the lowest
    rung — return the lowest rung (the player would still try, then degrade).

    This is the server-side mirror of the client decision, used to pre-warm /
    prioritise the transcode of the rung the reader is most likely to request.

    Raises:
        LadderError: if the ladder is empty.
    """
    if not ladder:
        raise LadderError("cannot select from an empty ladder")
    ordered = sort_ladder(ladder)  # highest first
    budget = available_bps * headroom
    for rendition in ordered:
        if rendition.peak_bandwidth_bps <= budget:
            return rendition
    return ordered[-1]  # nothing fits → lowest rung


def validate_ladder(ladder: Sequence[Rendition]) -> None:
    """Assert a ladder is non-empty and free of duplicate resolutions.

    Raises:
        LadderError: on an empty ladder or a duplicate ``WxH`` rung.
    """
    if not ladder:
        raise LadderError("ladder is empty")
    resolutions = [r.resolution for r in ladder]
    if len(set(resolutions)) != len(resolutions):
        raise LadderError(f"duplicate resolutions in ladder: {resolutions}")
