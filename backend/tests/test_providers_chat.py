"""Unit tests for the chat provider: completion parsing, token accounting,
tool-call extraction, and the JSON-repair retry path. No network."""

from __future__ import annotations

import json

import httpx
import pytest

from app.providers.chat import ChatProvider, extract_json
from app.providers.errors import ProviderBadRequest, ResponseParseError
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
    value = await ChatProvider(client).chat_json(
        [{"role": "user", "content": "x"}], "qwen3.7-plus", stream=False
    )
    assert value == {"beats": 3}
    assert handler.calls == 1
    await client.aclose()


async def test_chat_json_repairs_once_on_bad_json() -> None:
    handler = _Sequencer([_chat_response("oops not json"), _chat_response('{"ok": true}')])
    client = make_client(handler)
    value = await ChatProvider(client).chat_json(
        [{"role": "user", "content": "x"}], "qwen3.7-plus", stream=False
    )
    assert value == {"ok": True}
    assert handler.calls == 2  # original + one repair
    await client.aclose()


async def test_chat_json_raises_after_failed_repair() -> None:
    handler = _Sequencer([_chat_response("nope"), _chat_response("still nope")])
    client = make_client(handler)
    with pytest.raises(ResponseParseError):
        await ChatProvider(client).chat_json(
            [{"role": "user", "content": "x"}], "qwen3.7-plus", stream=False
        )
    assert handler.calls == 2
    await client.aclose()


# --------------------------------------------------------------------------- #
# Streaming (SSE) — the ~60s gateway-timeout fix
# --------------------------------------------------------------------------- #


def _chunk(
    content: str | None = None,
    *,
    finish: str | None = None,
    tool_calls: list[dict] | None = None,
    role: str | None = None,
) -> dict:
    delta: dict = {}
    if role is not None:
        delta["role"] = role
    if content is not None:
        delta["content"] = content
    if tool_calls is not None:
        delta["tool_calls"] = tool_calls
    return {
        "id": "chatcmpl-stream",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }


def _sse_bytes(chunks: list[dict], *, usage: dict | None = None) -> bytes:
    # Intersperse keep-alive comments and a stray blank line to prove tolerance.
    parts = [": keep-alive\n\n"]
    for chunk in chunks:
        parts.append(f"data: {json.dumps(chunk)}\n\n")
    if usage is not None:
        final = {"id": "chatcmpl-stream", "choices": [], "usage": usage}
        parts.append(f"data: {json.dumps(final)}\n\n")
    parts.append("\n")  # stray blank line
    parts.append("data: [DONE]\n\n")
    return "".join(parts).encode()


class _SSESequencer:
    """Serves each queued SSE body once (then repeats the last); captures payload."""

    def __init__(self, bodies: list[bytes]) -> None:
        self.bodies = bodies
        self.calls = 0
        self.last_payload: dict = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.last_payload = json.loads(request.content)
        idx = min(self.calls, len(self.bodies) - 1)
        self.calls += 1
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=self.bodies[idx],
        )


async def test_chat_stream_accumulates_deltas_and_usage() -> None:
    body = _sse_bytes(
        [
            _chunk(role="assistant"),
            _chunk("Hello"),
            _chunk(" world"),
            _chunk(finish="stop"),
        ],
        usage={"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
    )
    handler = _SSESequencer([body])
    client = make_client(handler)
    result = await ChatProvider(client).chat(
        [{"role": "user", "content": "hi"}], "qwen3.5-plus", stream=True
    )
    assert result.text == "Hello world"
    assert result.finish_reason == "stop"
    assert (result.input_tokens, result.output_tokens) == (7, 3)
    # The request opted into streaming + usage in the final chunk.
    assert handler.last_payload["stream"] is True
    assert handler.last_payload["stream_options"] == {"include_usage": True}
    totals = client.usage_totals
    assert totals is not None and totals.total_tokens == 10
    await client.aclose()


async def test_chat_stream_aggregates_tool_call_deltas() -> None:
    tool_first = [
        {
            "index": 0,
            "id": "c1",
            "type": "function",
            "function": {"name": "make_shot", "arguments": '{"a'},
        }
    ]
    tool_second = [{"index": 0, "function": {"arguments": '":1}'}}]
    body = _sse_bytes(
        [
            _chunk(tool_calls=tool_first),
            _chunk(tool_calls=tool_second),
            _chunk(finish="tool_calls"),
        ],
        usage={"prompt_tokens": 5, "completion_tokens": 8},
    )
    client = make_client(_SSESequencer([body]))
    result = await ChatProvider(client).chat(
        [{"role": "user", "content": "go"}], "qwen3.7-max", stream=True
    )
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].name == "make_shot"
    assert result.tool_calls[0].arguments == '{"a":1}'  # deltas concatenated across chunks
    await client.aclose()


async def test_chat_json_streams_by_default() -> None:
    handler = _SSESequencer([_sse_bytes([_chunk('{"beats": [1, 2, 3]}'), _chunk(finish="stop")])])
    client = make_client(handler)
    value = await ChatProvider(client).chat_json([{"role": "user", "content": "x"}], "qwen3.5-plus")
    assert value == {"beats": [1, 2, 3]}
    # No stream arg -> defaulted to streaming (the transparent timeout fix).
    assert handler.last_payload["stream"] is True
    assert handler.last_payload["response_format"] == {"type": "json_object"}
    await client.aclose()


async def test_chat_json_stream_passes_enable_thinking() -> None:
    handler = _SSESequencer([_sse_bytes([_chunk('{"ok": true}'), _chunk(finish="stop")])])
    client = make_client(handler)
    await ChatProvider(client).chat_json(
        [{"role": "user", "content": "x"}], "qwen3.5-plus", enable_thinking=False
    )
    assert handler.last_payload["enable_thinking"] is False
    await client.aclose()


async def test_chat_json_stream_repairs_once() -> None:
    bodies = [
        _sse_bytes([_chunk("oops not json"), _chunk(finish="stop")]),
        _sse_bytes([_chunk('{"ok": true}'), _chunk(finish="stop")]),
    ]
    handler = _SSESequencer(bodies)
    client = make_client(handler)
    value = await ChatProvider(client).chat_json([{"role": "user", "content": "x"}], "qwen3.5-plus")
    assert value == {"ok": True}
    assert handler.calls == 2  # streamed original + streamed repair
    await client.aclose()


async def test_chat_stream_handles_split_json_across_chunks() -> None:
    # The structured payload arrives token-by-token; assembly must reconstruct it.
    pieces = ['{"be', "ats", '": [', "1, 2", "]}"]
    body = _sse_bytes([_chunk(p) for p in pieces] + [_chunk(finish="stop")])
    client = make_client(_SSESequencer([body]))
    value = await ChatProvider(client).chat_json(
        [{"role": "user", "content": "x"}], "qwen3.5-plus", stream=True
    )
    assert value == {"beats": [1, 2]}
    await client.aclose()


async def test_chat_stream_4xx_raises_without_retry() -> None:
    state = {"calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["calls"] += 1
        return httpx.Response(400, json={"error": {"code": "InvalidParameter", "message": "bad"}})

    client = make_client(handler)
    with pytest.raises(ProviderBadRequest):
        await ChatProvider(client).chat(
            [{"role": "user", "content": "x"}], "qwen3.5-plus", stream=True
        )
    assert state["calls"] == 1  # 4xx is not retried
    await client.aclose()


async def test_chat_stream_retries_transient_then_succeeds() -> None:
    good = _sse_bytes(
        [_chunk("ok"), _chunk(finish="stop")], usage={"prompt_tokens": 1, "completion_tokens": 1}
    )
    state = {"calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["calls"] += 1
        if state["calls"] == 1:
            return httpx.Response(503, json={"message": "temporarily unavailable"})
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=good)

    client = make_client(handler)
    result = await ChatProvider(client).chat(
        [{"role": "user", "content": "x"}], "qwen3.5-plus", stream=True
    )
    assert result.text == "ok"
    assert state["calls"] == 2  # one transient failure, one success
    await client.aclose()
