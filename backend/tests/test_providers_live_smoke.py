"""LIVE smoke tests — real DashScope calls. Skipped unless ``KINORA_LIVE_TESTS``
is set, so CI never runs them. Run locally with the real key:

    export DASHSCOPE_API_KEY=$(grep '^DASHSCOPE_API_KEY=' .env | cut -d= -f2-)
    KINORA_LIVE_TESTS=1 .venv/bin/python -m pytest tests/test_providers_live_smoke.py -s -rA

These are intentionally tiny (a 1-word chat, a 64x64 image describe, one small
image gen, a 3-word TTS clip). They NEVER submit a real Wan video render — only
the cheap model-id verification and the LiveVideoDisabled gate are exercised.
"""

from __future__ import annotations

import io
import os

import pytest

from app.core.config import Settings, get_settings
from app.providers import LiveVideoDisabled, WanMode, WanSpec, create_providers

pytestmark = pytest.mark.skipif(
    not os.getenv("KINORA_LIVE_TESTS"),
    reason="live DashScope smoke tests; set KINORA_LIVE_TESTS=1 to run",
)


def _tiny_png() -> bytes:
    from PIL import Image

    img = Image.new("RGB", (64, 64), (200, 70, 55))
    for x in range(20, 44):
        for y in range(20, 44):
            img.putpixel((x, y), (250, 240, 60))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


async def test_live_chat() -> None:
    providers = create_providers()
    try:
        result = await providers.chat.chat(
            [{"role": "user", "content": "Reply with exactly one word: hello"}],
            get_settings().chat_model_plus,
            max_tokens=16,
        )
        print(
            f"\n[CHAT] model={result.model} text={result.text!r} "
            f"tokens(in/out)={result.input_tokens}/{result.output_tokens}"
        )
        assert result.text.strip()
    finally:
        await providers.aclose()


async def test_live_vl_describe() -> None:
    providers = create_providers()
    try:
        text = await providers.vl.analyze(
            [_tiny_png()],
            "What two colors dominate this image? Answer in 3 words.",
            max_tokens=64,
        )
        print(f"\n[VL] qwen-vl-max -> {text!r}")
        assert text.strip()
    finally:
        await providers.aclose()


async def test_live_image_generate() -> None:
    providers = create_providers()
    try:
        images = await providers.image.generate(
            "a single small red apple on a white background, minimalist",
            size="1024*1024",
            n=1,
        )
        assert images and images[0][:8] == b"\x89PNG\r\n\x1a\n"
        head_bytes = images[0][:8]
        print(
            f"\n[IMAGE] generated {len(images)} image(s); "
            f"bytes={len(images[0])} head={head_bytes!r}"
        )
    finally:
        await providers.aclose()


async def test_live_tts_with_word_timestamps() -> None:
    providers = create_providers()
    try:
        result = await providers.tts.synthesize(
            "Red fox runs.",
            voice_id="Cherry",
            model="qwen3-tts-flash",
            timeout=180.0,
        )
        head = [(w.text, w.t_start, w.t_end) for w in result.word_timestamps[:4]]
        print(
            f"\n[TTS] model={result.model} audio_bytes={len(result.audio_bytes)} "
            f"sample_rate={result.sample_rate} duration_s={result.duration_s} "
            f"alignment={result.alignment}\n      words={head}"
        )
        assert len(result.audio_bytes) > 1000
        assert result.word_timestamps  # REQUIRED for karaoke + page-turn
    finally:
        await providers.aclose()


async def test_live_video_verify_without_rendering() -> None:
    providers = create_providers()
    try:
        ok = await providers.video.verify_model_available()
        bogus = await providers.video.verify_model_available("wan-not-a-real-model-zzz")
        print(
            f"\n[VIDEO] verify_model_available({get_settings().video_model}) -> {ok}; "
            f"verify(bogus) -> {bogus} (no render submitted)"
        )
        assert ok is True
        assert bogus is False
    finally:
        await providers.aclose()


async def test_live_video_render_is_gated() -> None:
    # Force the gate closed regardless of the ambient .env value, then prove
    # render refuses to submit a real Wan task.
    settings = Settings(dashscope_api_key=get_settings().dashscope_api_key, kinora_live_video=False)
    providers = create_providers(settings)
    try:
        with pytest.raises(LiveVideoDisabled):
            await providers.video.render(
                WanSpec(mode=WanMode.TEXT_TO_VIDEO, prompt="a quiet meadow at dawn")
            )
        print("\n[VIDEO] render() correctly raised LiveVideoDisabled (no video-seconds spent)")
    finally:
        await providers.aclose()
