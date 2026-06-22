"""Unit tests for the chat provider: completion parsing, token accounting,
tool-call extraction, and the JSON-repair retry path. No network."""

from __future__ import annotations

import httpx
import pytest

from app.providers.chat import ChatProvider, extract_json
from app.providers.errors import ResponseParseError
from tests.test_providers_base import make_client


def _chat_response(content: str, *, tool_calls: list[dict] | None = None) -> dict:
    message: dict = {"role": "assistant", "content": content}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    return {
        "id": "chatcmpl-1",
        "choices": [{"message": message, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
    }


class _Sequencer:
    """Returns each queued JSON body once, then repeats the last."""

    def __init__(self, bodies: list[dict]) -> None:
        self.bodies = bodies
        self.calls = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        idx = min(self.calls, len(self.bodies) - 1)
        self.calls += 1
        return httpx.Response(200, json=self.bodies[idx])


# --------------------------------------------------------------------------- #
# extract_json
# --------------------------------------------------------------------------- #


def test_extract_json_plain() -> None:
    assert extract_json('{"a": 1}') == {"a": 1}


def test_extract_json_strips_code_fence() -> None:
    assert extract_json('```json\n{"a": 2}\n```') == {"a": 2}


def test_extract_json_recovers_from_surrounding_prose() -> None:
    assert extract_json('Sure! Here you go: {"k": [1, 2]} hope that helps') == {"k": [1, 2]}


def test_extract_json_raises_when_absent() -> None:
    with pytest.raises(ResponseParseError):
        extract_json("there is no json here")


# --------------------------------------------------------------------------- #
# chat
# --------------------------------------------------------------------------- #


async def test_chat_returns_text_and_records_usage() -> None:
    handler = _Sequencer([_chat_response("hello world")])
    client = make_client(handler)
    provider = ChatProvider(client)
    result = await provider.chat([{"role": "user", "content": "hi"}], "qwen3.7-plus")
    assert result.text == "hello world"
    assert result.finish_reason == "stop"
    assert (result.input_tokens, result.output_tokens) == (7, 3)
    totals = client.usage_totals
    assert totals is not None
    assert totals.by_operation == {"chat": 1}
    assert totals.total_tokens == 10
    await client.aclose()


async def test_chat_parses_tool_calls() -> None:
    tool_calls = [
        {"id": "c1", "type": "function", "function": {"name": "do_thing", "arguments": '{"x":1}'}}
    ]
    handler = _Sequencer([_chat_response("", tool_calls=tool_calls)])
    client = make_client(handler)
    result = await ChatProvider(client).chat([{"role": "user", "content": "go"}], "qwen3.7-max")
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].name == "do_thing"
    assert result.tool_calls[0].arguments == '{"x":1}'
    await client.aclose()


async def test_chat_json_parses_valid_first_try() -> None:
    handler = _Sequencer([_chat_response('{"beats": 3}')])
    client = make_client(handler)
    value = await ChatProvider(client).chat_json([{"role": "user", "content": "x"}], "qwen3.7-plus")
    assert value == {"beats": 3}
    assert handler.calls == 1
    await client.aclose()


async def test_chat_json_repairs_once_on_bad_json() -> None:
    handler = _Sequencer([_chat_response("oops not json"), _chat_response('{"ok": true}')])
    client = make_client(handler)
    value = await ChatProvider(client).chat_json([{"role": "user", "content": "x"}], "qwen3.7-plus")
    assert value == {"ok": True}
    assert handler.calls == 2  # original + one repair
    await client.aclose()


async def test_chat_json_raises_after_failed_repair() -> None:
    handler = _Sequencer([_chat_response("nope"), _chat_response("still nope")])
    client = make_client(handler)
    with pytest.raises(ResponseParseError):
        await ChatProvider(client).chat_json([{"role": "user", "content": "x"}], "qwen3.7-plus")
    assert handler.calls == 2
    await client.aclose()
