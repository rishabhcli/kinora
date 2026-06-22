"""Chat completions over DashScope's OpenAI-compatible endpoint.

Powers the Showrunner/Adapter/Continuity/Cinematographer agents (§11). Adds a
``chat_json`` helper that forces JSON-object output and repairs a single parse
failure with a terse "return ONLY valid JSON" reminder — the agents emit strict
JSON contracts (§10), so this keeps them robust without a full retry budget.

**Streaming.** The DashScope-intl gateway closes long *non-streaming* requests
at ~60s (``RemoteProtocolError: Server disconnected``), which breaks multi-beat
JSON generations on the qwen3 thinking models. The fix is to stream: with
``stream:true`` + ``stream_options:{include_usage:true}`` the connection stays
alive on a per-chunk (idle) read timeout instead of a single total cap, so a
2–3 minute thinking generation completes and token usage still arrives in the
final chunk. ``chat_json`` streams **by default** (structured/long generations
are exactly what timed out); plain ``chat`` keeps the non-streaming fast path
unless ``stream=True``.
"""

from __future__ import annotations

import json
from typing import Any

from .base import ProviderClient
from .errors import ResponseParseError
from .types import ChatResult, ToolCall, Usage

Messages = list[dict[str, Any]]
JsonValue = dict[str, Any] | list[Any]

_JSON_REMINDER = (
    "Your previous reply was not valid JSON. Return ONLY a single valid JSON "
    "value with no prose, no explanation, and no markdown code fences."
)


def extract_json(text: str) -> JsonValue:
    """Parse JSON from a model reply, tolerating code fences / surrounding prose.

    Raises:
        ResponseParseError: if no valid JSON object/array can be recovered.
    """
    candidate = text.strip()
    if candidate.startswith("```"):
        # Drop the opening fence (``` or ```json) and the trailing fence.
        candidate = candidate.split("\n", 1)[-1] if "\n" in candidate else candidate
        if candidate.endswith("```"):
            candidate = candidate[:-3]
        candidate = candidate.strip()
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        pass
    # Fall back to the first balanced {...} or [...] span.
    for open_ch, close_ch in (("{", "}"), ("[", "]")):
        start = candidate.find(open_ch)
        end = candidate.rfind(close_ch)
        if start != -1 and end > start:
            try:
                return json.loads(candidate[start : end + 1])
            except json.JSONDecodeError:
                continue
    raise ResponseParseError("model reply did not contain valid JSON")


class ChatProvider:
    """Async chat client over ``/compatible-mode/v1/chat/completions``."""

    def __init__(self, client: ProviderClient) -> None:
        self._client = client
        self._settings = client.settings

    @property
    def _chat_url(self) -> str:
        return f"{self._client.compat_base}/chat/completions"

    async def chat(
        self,
        messages: Messages,
        model: str,
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        response_format: dict[str, Any] | None = None,
        tools: list[dict[str, Any]] | None = None,
        timeout: float | None = None,
        stream: bool | None = None,
        enable_thinking: bool | None = None,
    ) -> ChatResult:
        """Run one chat completion and record token usage.

        Args:
            stream: Force the streaming SSE path (``True``) or the non-streaming
                fast path (``False``). Default (``None``) uses the non-streaming
                fast path; ``chat_json`` flips this default to streaming.
            enable_thinking: Optional Qwen passthrough to enable/disable the
                thinking phase (e.g. ``False`` for pure-extraction tasks). Not
                forced — streaming alone fixes the timeout even with thinking on.
            timeout: For the non-streaming path, the total per-call timeout. For
                the streaming path, the per-chunk *idle* read timeout.
        """
        payload = self._build_payload(
            messages, model, temperature, max_tokens, response_format, tools, enable_thinking
        )
        if stream:
            return await self._complete_stream(payload, model, idle_timeout=timeout)
        return await self._complete_nonstream(payload, model, timeout=timeout)

    async def chat_json(
        self,
        messages: Messages,
        model: str,
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        tools: list[dict[str, Any]] | None = None,
        timeout: float | None = None,
        stream: bool | None = None,
        enable_thinking: bool | None = None,
    ) -> JsonValue:
        """Chat constrained to JSON; repairs a single parse failure with a reminder.

        Streams by default (``stream=None`` -> ``True``) so the long structured
        generations that hit the ~60s gateway timeout now complete transparently
        for every caller (the Adapter included). Pass ``stream=False`` for the
        non-streaming fast path on short JSON replies.
        """
        use_stream = True if stream is None else stream
        result = await self.chat(
            messages,
            model,
            temperature=temperature,
            max_tokens=max_tokens,
            response_format={"type": "json_object"},
            tools=tools,
            timeout=timeout,
            stream=use_stream,
            enable_thinking=enable_thinking,
        )
        try:
            return extract_json(result.text)
        except ResponseParseError:
            repaired = [
                *messages,
                {"role": "assistant", "content": result.text},
                {"role": "user", "content": _JSON_REMINDER},
            ]
            retry = await self.chat(
                repaired,
                model,
                temperature=temperature,
                max_tokens=max_tokens,
                response_format={"type": "json_object"},
                tools=tools,
                timeout=timeout,
                stream=use_stream,
                enable_thinking=enable_thinking,
            )
            return extract_json(retry.text)

    # -- payload / transport ---------------------------------------------- #

    @staticmethod
    def _build_payload(
        messages: Messages,
        model: str,
        temperature: float | None,
        max_tokens: int | None,
        response_format: dict[str, Any] | None,
        tools: list[dict[str, Any]] | None,
        enable_thinking: bool | None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"model": model, "messages": messages}
        if temperature is not None:
            payload["temperature"] = temperature
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if response_format is not None:
            payload["response_format"] = response_format
        if tools:
            payload["tools"] = tools
        if enable_thinking is not None:
            payload["enable_thinking"] = enable_thinking
        return payload

    async def _complete_nonstream(
        self, payload: dict[str, Any], model: str, *, timeout: float | None
    ) -> ChatResult:
        body = await self._client.request_json(
            "POST", self._chat_url, op="chat", model=model, json=payload, timeout=timeout
        )
        return self._from_body(body, model)

    async def _complete_stream(
        self, payload: dict[str, Any], model: str, *, idle_timeout: float | None
    ) -> ChatResult:
        stream_payload = {**payload, "stream": True, "stream_options": {"include_usage": True}}
        events = await self._client.stream_sse(
            self._chat_url, op="chat", model=model, json=stream_payload, idle_timeout=idle_timeout
        )
        return self._from_stream(events, model)

    # -- response assembly ------------------------------------------------ #

    def _from_body(self, body: dict[str, Any], model: str) -> ChatResult:
        choices = body.get("choices") or []
        if not choices:
            raise ResponseParseError("chat response had no choices", request_id=body.get("id"))
        message = choices[0].get("message") or {}
        content = message.get("content")
        text = content if isinstance(content, str) else self._flatten_content(content)
        tool_calls = [
            ToolCall(
                id=tc.get("id"),
                type=tc.get("type", "function"),
                name=(tc.get("function") or {}).get("name", ""),
                arguments=(tc.get("function") or {}).get("arguments", "{}"),
            )
            for tc in (message.get("tool_calls") or [])
        ]
        return self._finalize(
            model,
            text=text,
            finish_reason=choices[0].get("finish_reason"),
            tool_calls=tool_calls,
            usage=body.get("usage") or {},
            request_id=body.get("id"),
        )

    def _from_stream(self, events: list[dict[str, Any]], model: str) -> ChatResult:
        text_parts: list[str] = []
        finish_reason: str | None = None
        usage: dict[str, Any] = {}
        request_id: str | None = None
        tool_acc: dict[int, dict[str, Any]] = {}
        for event in events:
            if request_id is None:
                request_id = event.get("id")
            event_usage = event.get("usage")
            if event_usage:
                usage = event_usage
            for choice in event.get("choices") or []:
                delta = choice.get("delta") or {}
                piece = delta.get("content")
                if isinstance(piece, str):
                    text_parts.append(piece)
                if choice.get("finish_reason"):
                    finish_reason = choice["finish_reason"]
                for tool_delta in delta.get("tool_calls") or []:
                    self._merge_tool_delta(tool_acc, tool_delta)
        if not events:
            raise ResponseParseError("chat stream produced no events")
        tool_calls = [
            ToolCall(
                id=slot["id"],
                type=slot["type"] or "function",
                name=slot["name"],
                arguments=slot["arguments"] or "{}",
            )
            for _, slot in sorted(tool_acc.items())
        ]
        return self._finalize(
            model,
            text="".join(text_parts),
            finish_reason=finish_reason,
            tool_calls=tool_calls,
            usage=usage,
            request_id=request_id,
        )

    @staticmethod
    def _merge_tool_delta(acc: dict[int, dict[str, Any]], tool_delta: dict[str, Any]) -> None:
        index = int(tool_delta.get("index", 0) or 0)
        slot = acc.setdefault(index, {"id": None, "type": "function", "name": "", "arguments": ""})
        if tool_delta.get("id"):
            slot["id"] = tool_delta["id"]
        if tool_delta.get("type"):
            slot["type"] = tool_delta["type"]
        function = tool_delta.get("function") or {}
        if function.get("name"):
            slot["name"] = function["name"]
        if function.get("arguments"):
            slot["arguments"] += function["arguments"]

    def _finalize(
        self,
        model: str,
        *,
        text: str,
        finish_reason: str | None,
        tool_calls: list[ToolCall],
        usage: dict[str, Any],
        request_id: str | None,
    ) -> ChatResult:
        result = ChatResult(
            text=text or "",
            model=model,
            finish_reason=finish_reason,
            tool_calls=tool_calls,
            input_tokens=int(usage.get("prompt_tokens", 0) or 0),
            output_tokens=int(usage.get("completion_tokens", 0) or 0),
        )
        self._client.record_usage(
            Usage(
                model=model,
                operation="chat",
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
                request_id=request_id,
            )
        )
        return result

    @staticmethod
    def _flatten_content(content: Any) -> str:
        if isinstance(content, list):
            return "".join(part.get("text", "") for part in content if isinstance(part, dict))
        return "" if content is None else str(content)


__all__ = ["ChatProvider", "extract_json"]
