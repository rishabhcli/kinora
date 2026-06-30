"""Ready-made graphs for the standard derived-media set of a finished clip.

A finished Kinora clip needs the same family of derivatives no matter which model
produced it (Wan, MiniMax, the Ken-Burns degradation lane): a normalised master,
a thumbnail, a poster, a preview GIF, a scrubbing sprite-sheet, a loudness-
normalised master, and — when captions / a watermark are supplied — burned-in
captions and a watermarked cut. This module wires those into a single
:class:`~app.video.mediagraph.graph.MediaGraph` whose independent branches fan out
in one parallel wave off the master, while the caption/watermark joins wait for
their two inputs.

These are *defaults*: callers can build any graph directly from the node classes.
The presets exist so the common case is one call.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.video.mediagraph.graph import MediaGraph
from app.video.mediagraph.nodes import (
    CaptionBurnInNode,
    ExtractLastFrameNode,
    LoudnessNormalizeNode,
    NormalizeNode,
    PosterNode,
    PreviewGifNode,
    ProbeNode,
    ScrubbingSpriteSheetNode,
    SourceNode,
    ThumbnailNode,
    WatermarkNode,
)
from app.video.mediagraph.types import FILM_GEOMETRY, ArtifactRef, Geometry, MediaKind


@dataclass(frozen=True, slots=True)
class DerivativesSpec:
    """Knobs for the standard derived-media graph (all optional, sane defaults)."""

    geometry: Geometry = FILM_GEOMETRY
    fps: int = 30
    crf: int = 20
    #: When set, a captions sidecar of this logical name is burned in.
    captions_input: str | None = None
    #: When set, this watermark-image logical input is overlaid.
    watermark_input: str | None = None
    #: Loudness target (EBU R128 integrated LUFS).
    target_lufs: float = -16.0
    #: Preview-GIF window.
    gif_start_s: float = 0.0
    gif_duration_s: float = 3.0
    #: Sprite-sheet grid.
    sprite_columns: int = 5
    sprite_rows: int = 5
    #: Include the probe node (metadata sidecar). Cheap; on by default.
    probe: bool = True


def build_derivatives_graph(
    spec: DerivativesSpec | None = None,
    *,
    source_name: str = "source",
    source_kind: MediaKind = MediaKind.VIDEO,
    source_ext: str = "mp4",
) -> MediaGraph:
    """The standard derived-media graph for a finished clip.

    Topology::

        source ─▶ master(normalize) ─┬─▶ thumb
                                     ├─▶ poster
                                     ├─▶ last_frame
                                     ├─▶ preview(gif)
                                     ├─▶ sprite(+manifest)
                                     ├─▶ loudnorm
                                     ├─▶ captioned   (join: master + captions)
                                     └─▶ watermarked (join: master + watermark)
        source ─▶ probe

    The thumbnail/poster/last-frame/gif/sprite/loudnorm branches are mutually
    independent, so they land in one parallel wave; the caption/watermark joins
    sit one wave later. ``probe`` reads the source directly (independent of the
    master). Returns a *validated* graph.
    """
    spec = spec or DerivativesSpec()
    g = MediaGraph()

    # The external source clip (supplied at run time).
    g.declare_external(ArtifactRef(name=source_name, kind=source_kind, ext=source_ext))
    g.add(SourceNode(node_id="source", media=source_kind, ext=source_ext, output_name=source_name))

    if spec.probe:
        g.add(ProbeNode(node_id="probe", source=source_name, out_name="probe"))

    # The canonical master every derivative reads.
    g.add(
        NormalizeNode(
            node_id="normalize",
            source=source_name,
            out_name="master",
            geometry=spec.geometry,
            fps=spec.fps,
            crf=spec.crf,
        )
    )

    # Independent derivatives off the master.
    thumb_geom = Geometry(width=spec.geometry.width // 2, height=spec.geometry.height // 2)
    g.add(
        ThumbnailNode(node_id="thumbnail", source="master", out_name="thumb", geometry=thumb_geom)
    )
    g.add(PosterNode(node_id="poster", source="master", out_name="poster", geometry=spec.geometry))
    g.add(ExtractLastFrameNode(node_id="last_frame", source="master", out_name="last_frame"))
    g.add(
        PreviewGifNode(
            node_id="preview_gif",
            source="master",
            out_name="preview",
            start_s=spec.gif_start_s,
            duration_s=spec.gif_duration_s,
        )
    )
    g.add(
        ScrubbingSpriteSheetNode(
            node_id="sprite_sheet",
            source="master",
            out_name="sprite",
            sidecar_name="sprite_manifest",
            columns=spec.sprite_columns,
            rows=spec.sprite_rows,
        )
    )
    g.add(
        LoudnessNormalizeNode(
            node_id="loudness",
            source="master",
            out_name="loudnorm",
            target_lufs=spec.target_lufs,
        )
    )

    # Optional joins.
    if spec.captions_input is not None:
        g.declare_external(
            ArtifactRef(name=spec.captions_input, kind=MediaKind.CAPTIONS, ext="vtt")
        )
        g.add(
            CaptionBurnInNode(
                node_id="caption_burn_in",
                source="master",
                captions=spec.captions_input,
                out_name="captioned",
            )
        )
    if spec.watermark_input is not None:
        g.declare_external(ArtifactRef(name=spec.watermark_input, kind=MediaKind.IMAGE, ext="png"))
        g.add(
            WatermarkNode(
                node_id="watermark",
                source="master",
                mark=spec.watermark_input,
                out_name="watermarked",
            )
        )

    g.validate()
    return g


__all__ = [
    "DerivativesSpec",
    "build_derivatives_graph",
]
