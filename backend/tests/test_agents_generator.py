"""Unit tests for the Generator bridge: render-mode→Wan mapping, WanSpec input
placement, and propagation of the LiveVideoDisabled spend gate. No network, no
real video."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.agents.contracts import RenderMode, ShotSpec
from app.agents.generator import Generator, build_wan_spec, wan_mode_for
from app.providers import LiveVideoDisabled, TtsResult, VideoResult, WanMode
from tests.test_agents_support import make_providers

_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


class _VideoBackend:
    name = "video:test"

    async def healthy(self) -> bool:
        return True

    async def render(self, spec: object) -> VideoResult:
        return VideoResult(
            duration_s=5,
            model="test-video",
            mode=WanMode.TEXT_TO_VIDEO,
            clip_bytes=b"provider-mp4",
        )


class _TtsBackend:
    async def synthesize(self, text: str, *, voice_id: str) -> TtsResult:
        return TtsResult(
            audio_bytes=b"wav",
            sample_rate=24000,
            duration_s=1,
            model="test-tts",
            voice_id=voice_id,
        )


class _Normalizer:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.seen: list[bytes] = []

    async def normalize_bytes_async(self, data: bytes) -> object:
        self.seen.append(data)
        if self.fail:
            raise RuntimeError("ffmpeg unavailable")
        return SimpleNamespace(clip_bytes=b"streaming-mp4")


def test_wan_mode_for_maps_every_render_mode() -> None:
    for render_mode in RenderMode:
        wan = wan_mode_for(render_mode)
        assert isinstance(wan, WanMode)
        assert wan.value == render_mode.value  # 1:1 by value


def test_build_wan_spec_reference_to_video_places_refs() -> None:
    spec = ShotSpec(shot_id="s1", render_mode=RenderMode.REFERENCE_TO_VIDEO, prompt="p", seed=5)
    wan = build_wan_spec(spec, reference_image_bytes=[_PNG])
    assert wan.mode is WanMode.REFERENCE_TO_VIDEO
    assert len(wan.reference_image_urls) == 1
    assert wan.reference_image_urls[0].startswith("data:image/png;base64,")
    assert wan.seed == 5
    assert wan.image_url is None


def test_build_wan_spec_first_last_frame_places_endpoints() -> None:
    spec = ShotSpec(shot_id="s2", render_mode=RenderMode.FIRST_LAST_FRAME, prompt="p")
    wan = build_wan_spec(spec, reference_image_bytes=[_PNG], prev_last_frame_bytes=_PNG)
    assert wan.mode is WanMode.FIRST_LAST_FRAME
    assert wan.first_frame_url is not None
    assert wan.last_frame_url is not None


def test_build_wan_spec_text_to_video_has_no_image_inputs() -> None:
    spec = ShotSpec(shot_id="s3", render_mode=RenderMode.TEXT_TO_VIDEO, prompt="a quiet meadow")
    wan = build_wan_spec(spec)
    assert wan.mode is WanMode.TEXT_TO_VIDEO
    assert wan.reference_image_urls == []
    assert wan.image_url is None
    assert wan.first_frame_url is None


def test_build_wan_spec_continuation_uses_prev_frame() -> None:
    spec = ShotSpec(shot_id="s4", render_mode=RenderMode.VIDEO_CONTINUATION, prompt="p")
    wan = build_wan_spec(spec, prev_last_frame_bytes=_PNG)
    assert wan.mode is WanMode.VIDEO_CONTINUATION
    assert wan.image_url is not None and wan.image_url.startswith("data:image/")


async def test_render_propagates_live_video_disabled_when_gated_off() -> None:
    gated = make_providers(live_video=False)
    try:
        generator = Generator(gated)
        spec = ShotSpec(shot_id="s5", render_mode=RenderMode.TEXT_TO_VIDEO, prompt="a meadow")
        with pytest.raises(LiveVideoDisabled):
            await generator.render(spec, narration_text="A quiet meadow.", voice_id="Cherry")
    finally:
        await gated.aclose()


async def test_generator_accepts_an_injected_video_backend_router() -> None:
    """A multi-backend ``VideoRouter`` drops in behind the Generator: the gate is
    still propagated unchanged (no clip fabricated, no second backend tried)."""
    from app.providers import VideoRouter, create_video_router

    gated = make_providers(live_video=False)
    try:
        router = create_video_router(
            gated.client, model_ids=["wan2.1-t2v-turbo", "wan2.5-t2v-preview"]
        )
        assert isinstance(router, VideoRouter)
        generator = Generator(gated, video_backend=router)
        spec = ShotSpec(shot_id="s6", render_mode=RenderMode.TEXT_TO_VIDEO, prompt="a meadow")
        with pytest.raises(LiveVideoDisabled):
            await generator.render(spec, narration_text="A quiet meadow.", voice_id="Cherry")
        # The gate is not a fault → the preferred backend stays healthy.
        assert router.health("video:wan2.1-t2v-turbo").total_failures == 0
    finally:
        await gated.aclose()


async def test_video_provider_satisfies_video_backend_protocol() -> None:
    """The single hosted ``VideoProvider`` is a drop-in ``VideoBackend`` (name +
    render + healthy), so wrapping one in a router needs no adapter; ``healthy()``
    is a no-network ``True`` while the gate is off."""
    from app.providers import VideoProvider
    from app.providers.video_router import VideoBackend

    gated = make_providers(live_video=False)
    try:
        assert isinstance(gated.video, VideoProvider)
        assert isinstance(gated.video, VideoBackend)  # runtime_checkable protocol
        assert gated.video.name.startswith("video:")
        assert await gated.video.healthy() is True  # gated-off probe is a no-op True
    finally:
        await gated.aclose()


async def test_render_normalizes_provider_clip_before_returning() -> None:
    normalizer = _Normalizer()
    providers = SimpleNamespace(video=_VideoBackend(), tts=_TtsBackend())
    generator = Generator(providers, normalizer=normalizer)  # type: ignore[arg-type]

    result = await generator.render(
        ShotSpec(shot_id="s7", render_mode=RenderMode.TEXT_TO_VIDEO, prompt="a meadow"),
        narration_text="A quiet meadow.",
        voice_id="Cherry",
    )

    assert normalizer.seen == [b"provider-mp4"]
    assert result.clip_bytes == b"streaming-mp4"


async def test_render_keeps_real_provider_clip_when_normalization_fails() -> None:
    normalizer = _Normalizer(fail=True)
    providers = SimpleNamespace(video=_VideoBackend(), tts=_TtsBackend())
    generator = Generator(providers, normalizer=normalizer)  # type: ignore[arg-type]

    result = await generator.render(
        ShotSpec(shot_id="s8", render_mode=RenderMode.TEXT_TO_VIDEO, prompt="a meadow"),
        narration_text="A quiet meadow.",
        voice_id="Cherry",
    )

    assert result.clip_bytes == b"provider-mp4"
