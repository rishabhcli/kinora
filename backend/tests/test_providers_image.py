"""Unit tests for the image provider: generation + edit. The SDK call is
monkeypatched to return a signed URL; the asset download is served by a
``MockTransport``. No network."""

from __future__ import annotations

import httpx
import pytest

from app.providers.image import ImageProvider
from tests.test_providers_base import make_client

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"generated-image-bytes"


class _FakeImageResp:
    def __init__(self, url: str) -> None:
        self.status_code = 200
        self.code = None
        self.message = None
        self.request_id = "img-req"
        self.output = {"choices": [{"message": {"content": [{"image": url}]}}]}
        self.usage = {"image_count": 1, "width": 1024, "height": 1024}


def _asset_handler(request: httpx.Request) -> httpx.Response:
    if request.url.host == "assets.test":
        return httpx.Response(200, content=PNG_BYTES)
    return httpx.Response(200, json={})


async def test_generate_returns_bytes_and_records_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    import dashscope

    calls: list[dict] = []

    def fake_call(**kwargs: object) -> _FakeImageResp:
        calls.append(kwargs)
        return _FakeImageResp("https://assets.test/keyframe.png")

    monkeypatch.setattr(dashscope.MultiModalConversation, "call", fake_call)
    client = make_client(_asset_handler)
    images = await ImageProvider(client).generate(
        "a fox", size="512*512", negative_prompt="blurry", seed=7
    )
    assert images == [PNG_BYTES]
    # Verify the request carried our params through to the SDK.
    assert calls[0]["model"] == client.settings.image_model
    assert calls[0]["size"] == "512*512"
    assert calls[0]["negative_prompt"] == "blurry"
    assert calls[0]["seed"] == 7
    totals = client.usage_totals
    assert totals is not None and totals.images == 1
    await client.aclose()


async def test_generate_n_loops_per_image(monkeypatch: pytest.MonkeyPatch) -> None:
    import dashscope

    seeds: list[object] = []

    def fake_call(**kwargs: object) -> _FakeImageResp:
        seeds.append(kwargs.get("seed"))
        return _FakeImageResp("https://assets.test/k.png")

    monkeypatch.setattr(dashscope.MultiModalConversation, "call", fake_call)
    client = make_client(_asset_handler)
    images = await ImageProvider(client).generate("x", n=3, seed=100)
    assert len(images) == 3
    assert seeds == [100, 101, 102]  # seed offset per draw
    await client.aclose()


async def test_edit_returns_single_image(monkeypatch: pytest.MonkeyPatch) -> None:
    import dashscope

    captured: list[dict] = []

    def fake_call(**kwargs: object) -> _FakeImageResp:
        captured.append(kwargs)
        return _FakeImageResp("https://assets.test/edited.png")

    monkeypatch.setattr(dashscope.MultiModalConversation, "call", fake_call)
    client = make_client(_asset_handler)
    out = await ImageProvider(client).edit(PNG_BYTES, "make the coat red")
    assert out == PNG_BYTES
    content = captured[0]["messages"][0]["content"]
    assert content[0]["image"].startswith("data:image/png;base64,")
    assert content[1] == {"text": "make the coat red"}
    assert captured[0]["model"] == client.settings.image_edit_model
    await client.aclose()
