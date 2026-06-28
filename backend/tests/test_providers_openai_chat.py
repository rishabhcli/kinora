"""Unit tests for the OpenAI reasoning chat provider (GPT-5 line) and the
``reasoning_provider`` routing toggle. No network: HTTP goes through
``httpx.MockTransport``."""

from __future__ import annotations

import json

import httpx
import pytest

from app.core.config import Settings
from app.providers import create_providers
from app.providers.chat import ChatProvider, OpenAIChatProvider
from tests.test_providers_base import make_client


def _oai_response(content: str) -> dict:
    return {
        "id": "chatcmpl-oai",
        "model": "gpt-5.5-2026-04-23",
        "choices": [
            {"message": {"role": "assistant", "content": content}, "finish_reason": "stop"}
        ],
        "usage": {"prompt_tokens": 11, "completion_tokens": 16, "total_tokens": 27},
    }


class _Capture:
    """Captures the outgoing request (url, auth header, JSON payload)."""

    def __init__(self, body: dict) -> None:
        self.body = body
        self.url: str | None = None
        self.auth: str | None = None
        self.payload: dict = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.url = str(request.url)
        self.auth = request.headers.get("authorization")
        self.payload = json.loads(request.content)
        return httpx.Response(200, json=self.body)


def _openai_client(handler: _Capture):
    return make_client(
        handler,
        base_url_override="https://api.openai.com/v1",
        api_key_override="sk-test-openai",
    )


async def test_openai_chat_hits_native_endpoint_with_bearer() -> None:
    cap = _Capture(_oai_response("ok"))
    client = _openai_client(cap)
    provider = OpenAIChatProvider(client, model="gpt-5.5", reasoning_effort="high")
    result = await provider.chat(
        [{"role": "user", "content": "hi"}], "qwen3.7-plus", stream=False
    )
    assert result.text == "ok"
    # Native OpenAI endpoint (no DashScope /compatible-mode/v1 segment).
    assert cap.url == "https://api.openai.com/v1/chat/completions"
    assert cap.auth == "Bearer sk-test-openai"
    await client.aclose()


async def test_openai_payload_is_reshaped_for_reasoning() -> None:
    cap = _Capture(_oai_response("ok"))
    client = _openai_client(cap)
    provider = OpenAIChatProvider(
        client, model="gpt-5.5", reasoning_effort="high", max_output_tokens=8192
    )
    await provider.chat(
        [{"role": "user", "content": "hi"}],
        "qwen3.7-plus",  # caller passes a Qwen id...
        temperature=0.7,
        max_tokens=64,
        enable_thinking=True,
        stream=False,
    )
    p = cap.payload
    assert p["model"] == "gpt-5.5"  # ...forced to the configured reasoning model
    assert p["reasoning_effort"] == "high"
    assert p["max_completion_tokens"] == 8192  # floored to reasoning headroom
    assert "max_tokens" not in p
    assert "temperature" not in p  # reasoning models reject a non-default value
    assert "enable_thinking" not in p  # Qwen-only passthrough
    await client.aclose()


async def test_openai_max_completion_tokens_respects_larger_request() -> None:
    cap = _Capture(_oai_response("ok"))
    client = _openai_client(cap)
    await OpenAIChatProvider(client, model="gpt-5.5", max_output_tokens=4000).chat(
        [{"role": "user", "content": "hi"}], "qwen3.7-plus", max_tokens=10000, stream=False
    )
    assert cap.payload["max_completion_tokens"] == 10000
    await client.aclose()


async def test_openai_usage_recorded_under_reasoning_model() -> None:
    cap = _Capture(_oai_response("ok"))
    client = _openai_client(cap)
    await OpenAIChatProvider(client, model="gpt-5.5").chat(
        [{"role": "user", "content": "hi"}], "qwen3.7-plus", stream=False
    )
    totals = client.usage_totals
    assert totals is not None
    assert totals.total_tokens == 27
    assert totals.by_operation == {"chat": 1}
    await client.aclose()


async def test_openai_chat_json_inherits_repair_and_forces_model() -> None:
    # First reply is not JSON -> one repair; both calls must target gpt-5.5.
    seen_models: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        seen_models.append(payload["model"])
        content = "not json" if len(seen_models) == 1 else '{"ok": true}'
        return httpx.Response(200, json=_oai_response(content))

    client = make_client(
        handler,
        base_url_override="https://api.openai.com/v1",
        api_key_override="sk-test-openai",
    )
    value = await OpenAIChatProvider(client, model="gpt-5.5").chat_json(
        [{"role": "user", "content": "x"}], "qwen3.7-plus", stream=False
    )
    assert value == {"ok": True}
    assert seen_models == ["gpt-5.5", "gpt-5.5"]
    await client.aclose()


# --------------------------------------------------------------------------- #
# Routing toggle (create_providers) + config validation
# --------------------------------------------------------------------------- #


async def test_create_providers_routes_reasoning_to_openai() -> None:
    settings = Settings(
        dashscope_api_key="test",
        reasoning_provider="openai",
        openai_api_key="sk-test",
    )
    providers = create_providers(settings)
    assert isinstance(providers.chat, OpenAIChatProvider)
    assert providers.reasoning_client is not None
    # Native providers stay on the DashScope client.
    assert providers.image._client is providers.client
    assert providers.chat._client is providers.reasoning_client
    await providers.aclose()


async def test_create_providers_defaults_to_dashscope() -> None:
    providers = create_providers(
        Settings(dashscope_api_key="test", reasoning_provider="dashscope")
    )
    assert isinstance(providers.chat, ChatProvider)
    assert not isinstance(providers.chat, OpenAIChatProvider)
    assert providers.reasoning_client is None
    assert providers.chat._client is providers.client
    await providers.aclose()


def test_openai_reasoning_requires_key() -> None:
    # Explicit ``None`` overrides any OPENAI_API_KEY picked up from .env.
    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        Settings(dashscope_api_key="test", reasoning_provider="openai", openai_api_key=None)


def test_invalid_reasoning_provider_rejected() -> None:
    with pytest.raises(ValueError, match="REASONING_PROVIDER"):
        Settings(dashscope_api_key="test", reasoning_provider="anthropic")
