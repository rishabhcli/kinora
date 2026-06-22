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
import time

import pytest

from app.core.config import Settings, get_settings
from app.providers import (
    EMBED_DIM,
    LiveVideoDisabled,
    WanMode,
    WanSpec,
    cosine,
    create_providers,
)

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


#: A multi-beat Adapter-style paragraph (Brothers Grimm, "The Frog King" —
#: public domain). On the qwen3 thinking models this structured JSON generation
#: previously hit the DashScope-intl ~60s non-streaming gateway cut-off.
_GRIMM_PARAGRAPH = (
    "In olden times when wishing still helped one, there lived a king whose "
    "daughters were all beautiful; and the youngest was so beautiful that the "
    "sun itself, which has seen so much, was astonished whenever it shone in "
    "her face. Close by the king's castle lay a great dark forest, and under an "
    "old lime-tree in the forest was a well; and when the day was very warm, the "
    "king's child went out into the forest and sat down by the side of the cool "
    "fountain; and when she was bored she took a golden ball, and threw it up on "
    "high and caught it, and this ball was her favourite plaything."
)


async def test_live_adapter_chat_json_streams_under_cap() -> None:
    """The previously-timing-out Adapter generation now succeeds via streaming."""
    providers = create_providers()
    messages = [
        {
            "role": "system",
            "content": (
                "You are a screenwriter adapting a book into a shot list. Output ONLY a "
                'JSON object of the form {"beats": [...]}. Each beat has "summary" (string), '
                '"entities" (array of names you can resolve), and "mood" (string). Produce 5 '
                "to 6 beats. No prose, no markdown fences."
            ),
        },
        {"role": "user", "content": _GRIMM_PARAGRAPH},
    ]
    try:
        started = time.perf_counter()
        # chat_json streams by default — thinking stays ON for qwen3.5-plus.
        result = await providers.chat.chat_json(messages, get_settings().chat_model_adapter)
        elapsed = time.perf_counter() - started
        totals = providers.client.usage_totals
        beats = result.get("beats") if isinstance(result, dict) else None
        print(
            f"\n[ADAPTER/stream] model={get_settings().chat_model_adapter} "
            f"elapsed={elapsed:.1f}s beats={len(beats) if beats else 0} "
            f"tokens(in/out)={totals.input_tokens}/{totals.output_tokens}"
        )
        assert isinstance(result, dict) and isinstance(beats, list) and len(beats) >= 5
        assert all("summary" in b for b in beats)
        print(f"        first beat: {beats[0]}")
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


async def test_live_embeddings_image_and_text() -> None:
    providers = create_providers()
    try:
        [img_vec] = await providers.embeddings.embed_images([_tiny_png()])
        [txt_vec] = await providers.embeddings.embed_texts(["a red square with a yellow center"])
        self_cos = cosine(img_vec, img_vec)
        cross_cos = cosine(img_vec, txt_vec)
        norm = sum(x * x for x in img_vec) ** 0.5
        print(
            f"\n[EMBED] model=tongyi-embedding-vision-plus dim={len(img_vec)} "
            f"(EMBED_DIM={EMBED_DIM}) unit_norm={norm:.4f}\n"
            f"        cosine(img,img)={self_cos:.4f} cosine(img,text)={cross_cos:.4f}"
        )
        assert len(img_vec) == EMBED_DIM == len(txt_vec)
        assert abs(norm - 1.0) < 1e-3
        assert self_cos == pytest.approx(1.0, abs=1e-4)
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
