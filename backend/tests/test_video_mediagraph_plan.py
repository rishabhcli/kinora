"""The pure PLAN layer — exact ffmpeg arg-plan per node + intermediate graph.

No ffmpeg, no subprocess. The plan is byte-for-byte deterministic given a fixed
work dir and source path, so these assert on the literal argument vectors each
node emits and on how intermediate artifacts wire the nodes together.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.video.mediagraph.graph import MediaGraph
from app.video.mediagraph.nodes import (
    CaptionBurnInNode,
    ExtractLastFrameNode,
    LoudnessNormalizeNode,
    NormalizeNode,
    PlanContext,
    PosterNode,
    PreviewGifNode,
    ProbeNode,
    ScrubbingSpriteSheetNode,
    SourceNode,
    ThumbnailNode,
    WatermarkCorner,
    WatermarkNode,
)
from app.video.mediagraph.plan import build_plan
from app.video.mediagraph.types import ArtifactRef, Geometry, MediaKind

WORK = Path("/work")
SRC = Path("/in/clip.mp4")


def _ctx(**inputs: Path) -> PlanContext:
    return PlanContext(inputs=inputs, out_dir=WORK)


# --------------------------------------------------------------------------- #
# Per-node arg-plan (built directly against a PlanContext)
# --------------------------------------------------------------------------- #


def test_probe_node_arg_plan() -> None:
    node = ProbeNode(node_id="probe", source="source", out_name="probe")
    (inv,) = node.build_invocations(_ctx(source=SRC))
    assert inv.binary == "ffprobe"
    assert inv.captures_stdout is True
    assert inv.args == (
        "-v",
        "error",
        "-show_format",
        "-show_streams",
        "-of",
        "json",
        str(SRC),
    )
    assert inv.produces is not None and inv.produces.name == "probe"


def test_normalize_node_letterbox_arg_plan() -> None:
    node = NormalizeNode(
        node_id="n",
        source="source",
        out_name="master",
        geometry=Geometry(width=720, height=1280),
        fps=30,
        crf=20,
    )
    (inv,) = node.build_invocations(_ctx(source=SRC))
    args = inv.args
    assert args[:3] == ("-y", "-i", str(SRC))
    vf = args[args.index("-vf") + 1]
    assert "scale=720:1280:force_original_aspect_ratio=decrease" in vf
    assert "pad=720:1280" in vf
    assert "fps=30" in vf
    assert args[args.index("-crf") + 1] == "20"
    assert "libx264" in args
    assert args[-1] == str(WORK / "master.mp4")


def test_normalize_crop_mode_uses_crop_not_pad() -> None:
    node = NormalizeNode(node_id="n", source="source", crop=True)
    (inv,) = node.build_invocations(_ctx(source=SRC))
    vf = inv.args[inv.args.index("-vf") + 1]
    assert "force_original_aspect_ratio=increase" in vf
    assert "crop=" in vf
    assert "pad=" not in vf


def test_extract_last_frame_uses_sseof() -> None:
    node = ExtractLastFrameNode(node_id="lf", source="source", out_name="last")
    (inv,) = node.build_invocations(_ctx(source=SRC))
    assert "-sseof" in inv.args
    assert inv.args[-1] == str(WORK / "last.png")


def test_extract_frame_at_offset_seeks() -> None:
    node = ExtractLastFrameNode(node_id="lf", source="source", out_name="f", at=4.5)
    (inv,) = node.build_invocations(_ctx(source=SRC))
    assert inv.args[inv.args.index("-ss") + 1] == "4.500"


def test_thumbnail_arg_plan_scales_and_pads() -> None:
    node = ThumbnailNode(
        node_id="t", source="source", out_name="thumb", geometry=Geometry(width=360, height=640)
    )
    (inv,) = node.build_invocations(_ctx(source=SRC))
    vf = inv.args[inv.args.index("-vf") + 1]
    assert vf.startswith("thumbnail,")
    assert "scale=360:640" in vf
    assert inv.args[-1] == str(WORK / "thumb.jpg")


def test_poster_keeps_full_geometry_png() -> None:
    node = PosterNode(node_id="p", source="source", geometry=Geometry(width=720, height=1280))
    (inv,) = node.build_invocations(_ctx(source=SRC))
    assert inv.args[-1].endswith("poster.png")
    assert "scale=720:1280" in inv.args[inv.args.index("-vf") + 1]


def test_preview_gif_is_two_pass_palette() -> None:
    node = PreviewGifNode(
        node_id="g", source="source", out_name="preview", fps=12, start_s=1.0, duration_s=2.0
    )
    invs = node.build_invocations(_ctx(source=SRC))
    assert len(invs) == 2
    gen, use = invs
    assert "palettegen=stats_mode=diff" in gen.args[gen.args.index("-vf") + 1]
    assert gen.produces is None  # the palette is an intermediate, not the product
    # The second pass consumes the palette the first produced and emits the GIF.
    assert any("paletteuse" in a for a in use.args)
    assert use.produces is not None and use.produces.name == "preview"
    assert use.args[-1] == str(WORK / "preview.gif")
    # Both passes seek/limit to the same window.
    assert gen.args[gen.args.index("-ss") + 1] == "1.000"
    assert use.args[use.args.index("-t") + 1] == "2.000"


def test_sprite_sheet_tiles_and_has_manifest() -> None:
    node = ScrubbingSpriteSheetNode(
        node_id="s", source="source", out_name="sprite", columns=4, rows=3
    )
    (inv,) = node.build_invocations(_ctx(source=SRC))
    vf = inv.args[inv.args.index("-vf") + 1]
    assert "tile=4x3" in vf
    assert node.tile_count == 12
    manifest = node.manifest()
    assert manifest["columns"] == 4 and manifest["rows"] == 3
    assert manifest["tile_count"] == 12
    assert manifest["sheet"] == "sprite.png"
    # Two declared outputs: the sheet image + the JSON sidecar.
    names = {o.name for o in node.outputs}
    assert names == {"sprite", "sprite_manifest"}


def test_caption_burn_in_escapes_subtitle_path() -> None:
    node = CaptionBurnInNode(node_id="b", source="master", captions="subs", out_name="captioned")
    inv = node.build_invocations(
        _ctx(master=Path("/work/master.mp4"), subs=Path("/work/subs.vtt"))
    )[0]
    vf = inv.args[inv.args.index("-vf") + 1]
    assert "subtitles=" in vf
    # The colon in the path is escaped for the filtergraph.
    assert "\\:" in vf or "/work/subs.vtt" in vf
    assert inv.args[inv.args.index("-c:a") + 1] == "copy"


def test_caption_burn_in_force_style() -> None:
    node = CaptionBurnInNode(node_id="b", source="master", captions="subs", style="FontSize=24")
    inv = node.build_invocations(_ctx(master=Path("/m.mp4"), subs=Path("/s.vtt")))[0]
    vf = inv.args[inv.args.index("-vf") + 1]
    assert "force_style='FontSize=24'" in vf


def test_loudness_video_remux_copies_video_stream() -> None:
    node = LoudnessNormalizeNode(node_id="l", source="master", target_lufs=-14.0)
    (inv,) = node.build_invocations(_ctx(master=Path("/m.mp4")))
    af = inv.args[inv.args.index("-af") + 1]
    assert "loudnorm=I=-14.0" in af
    assert inv.args[inv.args.index("-c:v") + 1] == "copy"
    assert inv.args[-1].endswith(".mp4")


def test_loudness_audio_only_drops_video() -> None:
    node = LoudnessNormalizeNode(node_id="l", source="master", audio_only=True)
    (inv,) = node.build_invocations(_ctx(master=Path("/m.mp4")))
    assert "-vn" in inv.args
    assert inv.args[-1].endswith(".m4a")
    assert node.outputs[0].kind is MediaKind.AUDIO


@pytest.mark.parametrize(
    ("corner", "xy"),
    [
        (WatermarkCorner.TOP_LEFT, "24:24"),
        (WatermarkCorner.TOP_RIGHT, "W-w-24:24"),
        (WatermarkCorner.BOTTOM_LEFT, "24:H-h-24"),
        (WatermarkCorner.BOTTOM_RIGHT, "W-w-24:H-h-24"),
    ],
)
def test_watermark_corner_overlay_xy(corner: WatermarkCorner, xy: str) -> None:
    node = WatermarkNode(node_id="wm", source="master", mark="logo", corner=corner, margin=24)
    (inv,) = node.build_invocations(_ctx(master=Path("/m.mp4"), logo=Path("/l.png")))
    fc = inv.args[inv.args.index("-filter_complex") + 1]
    assert f"overlay={xy}" in fc
    assert "colorchannelmixer=aa=0.85" in fc


# --------------------------------------------------------------------------- #
# Whole-graph plan + intermediate-artifact graph + determinism
# --------------------------------------------------------------------------- #


def _full_graph() -> MediaGraph:
    g = MediaGraph()
    g.declare_external(ArtifactRef(name="source", kind=MediaKind.VIDEO, ext="mp4"))
    g.add(SourceNode(node_id="source"))
    g.add(NormalizeNode(node_id="normalize", source="source", out_name="master"))
    g.add(ThumbnailNode(node_id="thumb", source="master", out_name="thumb"))
    g.add(PreviewGifNode(node_id="gif", source="master", out_name="preview"))
    return g


def test_plan_resolves_intermediate_paths_between_nodes() -> None:
    plan = build_plan(_full_graph(), work_dir=WORK, external_paths={"source": SRC})
    # The thumbnail reads the master the normalize node writes.
    thumb = plan.by_id("thumb")
    assert thumb.input_paths["master"] == WORK / "master.mp4"
    assert thumb.upstreams == ("normalize",)
    # The intermediate-artifact graph wires consumers to producers.
    igraph = plan.intermediate_graph()
    assert igraph["normalize"] == ("source",)
    assert igraph["thumb"] == ("normalize",)
    assert igraph["gif"] == ("normalize",)


def test_plan_artifact_paths_cover_every_output() -> None:
    plan = build_plan(_full_graph(), work_dir=WORK, external_paths={"source": SRC})
    paths = plan.artifact_paths()
    assert paths["source"] == SRC
    assert paths["master"] == WORK / "master.mp4"
    assert paths["thumb"] == WORK / "thumb.jpg"
    assert paths["preview"] == WORK / "preview.gif"


def test_plan_is_byte_for_byte_deterministic() -> None:
    a = build_plan(_full_graph(), work_dir=WORK, external_paths={"source": SRC})
    b = build_plan(_full_graph(), work_dir=WORK, external_paths={"source": SRC})
    assert [i.command() for i in a.invocations] == [i.command() for i in b.invocations]


def test_plan_explain_lists_waves_and_commands() -> None:
    text = build_plan(_full_graph(), work_dir=WORK, external_paths={"source": SRC}).explain()
    assert "wave 0: source" in text
    assert "ffmpeg" in text


def test_plan_missing_external_path_raises() -> None:
    # A true external with no producing node (captions) must be supplied or the
    # plan cannot resolve the join's input path.
    g = _full_graph()
    g.declare_external(ArtifactRef(name="subs", kind=MediaKind.CAPTIONS, ext="vtt"))
    g.add(CaptionBurnInNode(node_id="burn", source="master", captions="subs"))
    with pytest.raises(KeyError):
        build_plan(g, work_dir=WORK, external_paths={"source": SRC})  # 'subs' missing
