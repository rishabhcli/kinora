"""Tests for the MediaService orchestration facade.

The dedup / url / packaging-structure paths use the in-memory FakeMediaStore and
need no DB. The ffmpeg-backed derivation paths build a tiny real clip and skip
cleanly when ffmpeg is unavailable.
"""

from __future__ import annotations

import pytest

from app.media.kinds import MediaAssetKind
from app.media.service import MediaService, build_media_service
from app.media.store import MediaStore
from app.media.testing import FakeMediaStore, media_available, tiny_mp4
from app.media.transcode import Derivation, TranscodeJob


def _service(public: str | None = None) -> tuple[MediaService, FakeMediaStore]:
    backend = FakeMediaStore(public_base_url=public)
    return MediaService(MediaStore(backend)), backend


async def test_store_asset_dedups() -> None:
    svc, backend = _service()
    data = b"some-bytes"
    m1 = await svc.store_asset(data, kind=MediaAssetKind.POSTER, content_type="image/png")
    assert backend.put_calls == 1
    m2 = await svc.store_asset(data, kind=MediaAssetKind.POSTER, content_type="image/png")
    assert backend.put_calls == 1  # deduped
    assert m1.storage_key == m2.storage_key
    assert m1.storage_key.endswith(".png")


async def test_url_for_prefers_public_and_rewrites() -> None:
    svc, _ = _service(public="http://minio:9000/kinora")
    url = await svc.url_for("clips/b/s.mp4")
    assert url == "http://localhost:9000/kinora/clips/b/s.mp4"


async def test_gc_without_session_is_noop() -> None:
    svc, _ = _service()
    result = await svc.gc()
    assert result.collected == 0


def test_build_media_service_from_settings() -> None:
    from app.core.config import Settings

    settings = Settings(dashscope_api_key="test")
    backend = FakeMediaStore()
    svc = build_media_service(settings, object_store=backend)
    assert isinstance(svc, MediaService)


# -- ffmpeg-backed derivations ----------------------------------------------- #

requires_ffmpeg = pytest.mark.skipif(
    not media_available(), reason="ffmpeg not available for media service tests"
)


@pytest.fixture(scope="module")
def clip() -> bytes:
    return tiny_mp4(duration_s=2.0, width=120, height=160, fps=24)


@requires_ffmpeg
async def test_make_poster_stores_png(clip: bytes) -> None:
    svc, backend = _service()
    meta = await svc.make_poster(clip)
    assert meta.kind == MediaAssetKind.POSTER
    assert meta.content_type == "image/png"
    assert backend.get_bytes(meta.storage_key)[:8] == b"\x89PNG\r\n\x1a\n"


@requires_ffmpeg
async def test_make_sprite_stores_sheet_and_vtt(clip: bytes) -> None:
    svc, backend = _service(public="http://localhost:9000/kinora")
    sheet, vtt = await svc.make_sprite(clip, count=4)
    assert sheet.kind == MediaAssetKind.SPRITE
    assert vtt.kind == MediaAssetKind.VTT
    vtt_text = backend.get_bytes(vtt.storage_key).decode()
    assert vtt_text.startswith("WEBVTT")
    # the VTT references the stored sprite's public URL
    assert sheet.storage_key in vtt_text


@requires_ffmpeg
async def test_ingest_clip_probes(clip: bytes) -> None:
    svc, _ = _service()
    meta = await svc.ingest_clip(clip)
    assert meta.kind == MediaAssetKind.CLIP
    assert meta.width == 120
    assert meta.height == 160
    assert meta.duration_s is not None


@requires_ffmpeg
async def test_package_film_uploads_hls_package(clip: bytes) -> None:
    svc, backend = _service()
    master = await svc.package_film(clip)
    assert master.kind == MediaAssetKind.HLS
    assert master.storage_key.endswith("master.m3u8")
    assert master.meta["variants"]
    # the package's segment + variant files are all uploaded under the prefix
    prefix = master.storage_key.rsplit("/", 1)[0]
    uploaded = [k for k in backend.stored_keys() if k.startswith(prefix)]
    assert any(k.endswith(".ts") for k in uploaded)
    assert any(k.endswith("index.m3u8") for k in uploaded)
    # master playlist is fetchable + references a variant
    assert b"#EXTM3U" in backend.get_bytes(master.storage_key)


@requires_ffmpeg
async def test_run_transcode_job_derives_requested(clip: bytes) -> None:
    svc, backend = _service()
    # seed the source clip into the store
    src = await svc.ingest_clip(clip)
    job = TranscodeJob(
        source_key=src.storage_key,
        derivations=(Derivation.POSTER, Derivation.SPRITE),
    )
    out = await svc.run_transcode_job(job)
    assert set(out) == {Derivation.POSTER, Derivation.SPRITE}
    assert out[Derivation.POSTER].kind == MediaAssetKind.POSTER
    assert out[Derivation.SPRITE].kind == MediaAssetKind.SPRITE
