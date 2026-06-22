"""Unit tests for the VL provider: multimodal message construction (base64 for
local bytes, pass-through for URLs), text extraction, usage, JSON repair."""

from __future__ import annotations

import json

import httpx

from app.providers.vl import VLProvider, _sniff_mime
from tests.test_providers_base import make_client

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 16


def _vl_response(content: str) -> dict:
    return {
        "id": "vl-1",
        "choices": [
            {"message": {"role": "assistant", "content": content}, "finish_reason": "stop"}
        ],
        "usage": {"prompt_tokens": 20, "completion_tokens": 4, "total_tokens": 24},
    }


class _Capture:
    def __init__(self, content: str) -> None:
        self.content = content
        self.last_body: dict = {}
        self.calls = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        self.last_body = json.loads(request.content)
        return httpx.Response(200, json=_vl_response(self.content))


def test_sniff_mime() -> None:
    assert _sniff_mime(PNG) == "image/png"
    assert _sniff_mime(JPEG) == "image/jpeg"
    assert _sniff_mime(b"unknown") == "image/png"


async def test_analyze_encodes_bytes_and_passes_urls() -> None:
    cap = _Capture("Red and yellow.")
    client = make_client(cap)
    text = await VLProvider(client).analyze(
        [PNG, "https://img.test/p.png"],
        "what colors?",
    )
    assert text == "Red and yellow."
    content = cap.last_body["messages"][0]["content"]
    # two image parts + one text part
    assert content[0]["image_url"]["url"].startswith("data:image/png;base64,")
    assert content[1]["image_url"]["url"] == "https://img.test/p.png"
    assert content[2] == {"type": "text", "text": "what colors?"}
    assert cap.last_body["model"] == client.settings.vl_model
    totals = client.usage_totals
    assert totals is not None and totals.by_operation == {"vl": 1}
    await client.aclose()


async def test_analyze_json_repairs_once() -> None:
    class _Seq:
        def __init__(self) -> None:
            self.calls = 0

        def __call__(self, request: httpx.Request) -> httpx.Response:
            self.calls += 1
            body = _vl_response("not-json") if self.calls == 1 else _vl_response('{"ccs": 0.9}')
            return httpx.Response(200, json=body)

    seq = _Seq()
    client = make_client(seq)
    value = await VLProvider(client).analyze_json([PNG], "score it")
    assert value == {"ccs": 0.9}
    assert seq.calls == 2
    await client.aclose()
