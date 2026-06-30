"""ffmpeg-gated executor tests — the media graph produces REAL derivatives.

These run the actual ffmpeg/ffprobe via :class:`SubprocessRunner` and assert the
derived media is genuine: a normalised master at the target geometry, a real PNG
poster, a real animated GIF, a tiled sprite-sheet, an EBU-R128 loudness master,
and a probe JSON sidecar. Skipped when no ffmpeg binary is resolvable, so the
pure DAG/plan/cache suites remain runnable anywhere.

The source clip is a real Ken-Burns mp4 built with the bundled/system ffmpeg via
:mod:`app.render.degrade` (the project's existing real-asset builder).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.video.mediagraph.cache import InMemoryCacheStore, NullCacheStore
from app.video.mediagraph.engine import MediaGraphEngine
from app.video.mediagraph.graph import MediaGraph
from app.video.mediagraph.nodes import (
    LoudnessNormalizeNode,
    NormalizeNode,
    PosterNode,
    PreviewGifNode,
    ProbeNode,
    ScrubbingSpriteSheetNode,
    SourceNode,
    ThumbnailNode,
)
from app.video.mediagraph.presets import DerivativesSpec, build_derivatives_graph
from app.video.mediagraph.runner import SubprocessRunner, ffmpeg_available
from app.video.mediagraph.types import ArtifactRef, Geometry, MediaKind, NodeStatus

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(not ffmpeg_available(), reason="no ffmpeg binary available"),
]


def _real_source_clip(tmp_path: Path, *, duration_s: float = 2.0) -> Path:
    """A real mp4 (with audio) written to disk, built via the degrade lane."""
    from app.render import degrade
    from tests.test_render_support import png_bytes, wav_bytes

    clip = degrade.ken_burns_over_image(
        png_bytes(640, 360), duration_s, audio_bytes=wav_bytes(duration_s)
    )
    src = tmp_path / "source.mp4"
    src.write_bytes(clip)
    return src


async def test_normalize_produces_master_at_target_geometry(tmp_path: Path) -> None:
    src = _real_source_clip(tmp_path)
    g = MediaGraph()
    g.declare_external(ArtifactRef(name="source", kind=MediaKind.VIDEO, ext="mp4"))
    g.add(SourceNode(node_id="source"))
    g.add(
        NormalizeNode(
            node_id="normalize",
            source="source",
            out_name="master",
            geometry=Geometry(width=360, height=640),
            fps=24,
        )
    )
    g.validate()
    res = await MediaGraphEngine(runner=SubprocessRunner(), cache=NullCacheStore()).execute(
        g, work_dir=tmp_path / "out", external_paths={"source": src}
    )
    assert res.ok is True
    master = res.artifact("master")
    assert master is not None and master.path.exists()

    from app.render import degrade

    info = degrade.probe(master.path.read_bytes())
    assert info.has_video is True
    assert info.width == 360 and info.height == 640
    assert info.video_codec == "h264"


async def test_thumbnail_and_poster_are_real_images(tmp_path: Path) -> None:
    src = _real_source_clip(tmp_path)
    g = MediaGraph()
    g.declare_external(ArtifactRef(name="source", kind=MediaKind.VIDEO, ext="mp4"))
    g.add(SourceNode(node_id="source"))
    g.add(ThumbnailNode(node_id="thumb", source="source", out_name="thumb"))
    g.add(PosterNode(node_id="poster", source="source", out_name="poster"))
    g.validate()
    res = await MediaGraphEngine(runner=SubprocessRunner(), cache=NullCacheStore()).execute(
        g, work_dir=tmp_path / "out", external_paths={"source": src}
    )
    assert res.ok is True
    thumb = res.require_artifact("thumb")
    poster = res.require_artifact("poster")
    assert thumb.path.exists() and thumb.size_bytes > 0
    assert poster.path.exists() and poster.size_bytes > 0
    # PNG magic for the poster, JPEG SOI for the thumbnail.
    assert poster.path.read_bytes()[:4] == b"\x89PNG"
    assert thumb.path.read_bytes()[:2] == b"\xff\xd8"


async def test_preview_gif_is_a_real_gif(tmp_path: Path) -> None:
    src = _real_source_clip(tmp_path)
    g = MediaGraph()
    g.declare_external(ArtifactRef(name="source", kind=MediaKind.VIDEO, ext="mp4"))
    g.add(SourceNode(node_id="source"))
    g.add(PreviewGifNode(node_id="gif", source="source", out_name="preview", fps=8, duration_s=1.0))
    g.validate()
    res = await MediaGraphEngine(runner=SubprocessRunner(), cache=NullCacheStore()).execute(
        g, work_dir=tmp_path / "out", external_paths={"source": src}
    )
    assert res.ok is True
    gif = res.require_artifact("preview")
    assert gif.path.exists()
    assert gif.path.read_bytes()[:6] in (b"GIF87a", b"GIF89a")


async def test_sprite_sheet_and_manifest_are_real(tmp_path: Path) -> None:
    src = _real_source_clip(tmp_path, duration_s=3.0)
    g = MediaGraph()
    g.declare_external(ArtifactRef(name="source", kind=MediaKind.VIDEO, ext="mp4"))
    g.add(SourceNode(node_id="source"))
    g.add(
        ScrubbingSpriteSheetNode(
            node_id="sprite",
            source="source",
            out_name="sprite",
            sidecar_name="manifest",
            columns=2,
            rows=2,
            fps=1.0,
        )
    )
    g.validate()
    res = await MediaGraphEngine(runner=SubprocessRunner(), cache=NullCacheStore()).execute(
        g, work_dir=tmp_path / "out", external_paths={"source": src}
    )
    assert res.ok is True
    sheet = res.require_artifact("sprite")
    assert sheet.path.read_bytes()[:4] == b"\x89PNG"
    manifest = json.loads(res.require_artifact("manifest").path.read_text("utf-8"))
    assert manifest["columns"] == 2 and manifest["rows"] == 2
    assert manifest["tile_count"] == 4


async def test_loudness_normalises_audio_master(tmp_path: Path) -> None:
    src = _real_source_clip(tmp_path)
    g = MediaGraph()
    g.declare_external(ArtifactRef(name="source", kind=MediaKind.VIDEO, ext="mp4"))
    g.add(SourceNode(node_id="source"))
    g.add(LoudnessNormalizeNode(node_id="loud", source="source", out_name="loudnorm"))
    g.validate()
    res = await MediaGraphEngine(runner=SubprocessRunner(), cache=NullCacheStore()).execute(
        g, work_dir=tmp_path / "out", external_paths={"source": src}
    )
    assert res.ok is True
    from app.render import degrade

    info = degrade.probe(res.require_artifact("loudnorm").path.read_bytes())
    assert info.has_audio is True


async def test_probe_sidecar_holds_real_ffprobe_json(tmp_path: Path) -> None:
    src = _real_source_clip(tmp_path)
    g = MediaGraph()
    g.declare_external(ArtifactRef(name="source", kind=MediaKind.VIDEO, ext="mp4"))
    g.add(SourceNode(node_id="source"))
    g.add(ProbeNode(node_id="probe", source="source", out_name="probe"))
    g.validate()
    res = await MediaGraphEngine(runner=SubprocessRunner(), cache=NullCacheStore()).execute(
        g, work_dir=tmp_path / "out", external_paths={"source": src}
    )
    payload = json.loads(res.require_artifact("probe").path.read_text("utf-8"))
    assert "streams" in payload and "format" in payload


async def test_full_preset_produces_every_real_derivative(tmp_path: Path) -> None:
    src = _real_source_clip(tmp_path, duration_s=3.0)
    g = build_derivatives_graph(
        DerivativesSpec(geometry=Geometry(width=360, height=640), probe=True)
    )
    res = await MediaGraphEngine(runner=SubprocessRunner(), cache=InMemoryCacheStore()).execute(
        g, work_dir=tmp_path / "out", external_paths={"source": src}
    )
    assert res.ok is True, [
        (nid, r.status.value, r.error) for nid, r in res.results.items() if not r.status.is_ok
    ]
    arts = res.artifacts()
    for name in ("master", "thumb", "poster", "last_frame", "preview", "sprite", "loudnorm"):
        assert arts[name].path.exists(), name
        assert arts[name].size_bytes > 0, name


async def test_real_run_is_idempotent_via_cache(tmp_path: Path) -> None:
    src = _real_source_clip(tmp_path)
    g = build_derivatives_graph(DerivativesSpec(probe=True))
    cache = InMemoryCacheStore()
    out = tmp_path / "out"
    res1 = await MediaGraphEngine(runner=SubprocessRunner(), cache=cache).execute(
        g, work_dir=out, external_paths={"source": src}
    )
    assert res1.ok is True
    # Second run: every node with invocations is a cache hit (no ffmpeg).
    res2 = await MediaGraphEngine(runner=SubprocessRunner(), cache=cache).execute(
        g, work_dir=out, external_paths={"source": src}
    )
    assert res2.ok is True
    assert set(res2.cached) >= {"normalize", "thumbnail", "poster", "preview_gif"}
    # The cached artifacts point at the same on-disk files produced the first time.
    assert res2.require_artifact("master").path == res1.require_artifact("master").path


async def test_caption_burn_in_over_real_master(tmp_path: Path) -> None:
    src = _real_source_clip(tmp_path)
    subs = tmp_path / "subs.srt"
    subs.write_text("1\n00:00:00,000 --> 00:00:01,500\nHello mediagraph\n", "utf-8")
    g = build_derivatives_graph(DerivativesSpec(captions_input="captions", probe=False))
    res = await MediaGraphEngine(runner=SubprocessRunner(), cache=NullCacheStore()).execute(
        g,
        work_dir=tmp_path / "out",
        external_paths={"source": src, "captions": subs},
    )
    captioned = res.results["caption_burn_in"]
    # libass may be unavailable in some ffmpeg builds; accept a clean skip-free
    # success, else assert it failed in isolation without sinking the master.
    assert res.results["normalize"].status is NodeStatus.SUCCEEDED
    if captioned.status is NodeStatus.SUCCEEDED:
        assert res.require_artifact("captioned").path.exists()
    else:
        assert captioned.status is NodeStatus.FAILED
