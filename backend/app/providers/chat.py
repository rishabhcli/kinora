"""Chat completions over DashScope's OpenAI-compatible endpoint.

Powers the Showrunner/Adapter/Continuity/Cinematographer agents (§11). Adds a
``chat_json`` helper that forces JSON-object output and repairs a single parse
failure with a terse "return ONLY valid JSON" reminder — the agents emit strict
JSON contracts (§10), so this keeps them robust without a full retry budget.
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
    ) -> ChatResult:
        """Run one chat completion and record token usage."""
        payload: dict[str, Any] = {"model": model, "messages": messages}
        if temperature is not None:
            payload["temperature"] = temperature
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if response_format is not None:
            payload["response_format"] = response_format
        if tools:
            payload["tools"] = tools

        body = await self._client.request_json(
            "POST",
            f"{self._client.compat_base}/chat/completions",
            op="chat",
            model=model,
            json=payload,
            timeout=timeout,
        )
        return self._to_result(body, model)

    async def chat_json(
        self,
        messages: Messages,
        model: str,
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        tools: list[dict[str, Any]] | None = None,
        timeout: float | None = None,
    ) -> JsonValue:
        """Chat constrained to JSON; repairs a single parse failure with a reminder."""
        result = await self.chat(
            messages,
            model,
            temperature=temperature,
            max_tokens=max_tokens,
            response_format={"type": "json_object"},
            tools=tools,
            timeout=timeout,
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
            )
            return extract_json(retry.text)

    def _to_result(self, body: dict[str, Any], model: str) -> ChatResult:
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
        usage = body.get("usage") or {}
        result = ChatResult(
            text=text or "",
            model=model,
            finish_reason=choices[0].get("finish_reason"),
            tool_calls=tool_calls,
            input_tokens=int(usage.get("prompt_tokens", 0)),
            output_tokens=int(usage.get("completion_tokens", 0)),
        )
        self._client.record_usage(
            Usage(
                model=model,
                operation="chat",
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
                request_id=body.get("id"),
            )
        )
        return result

    @staticmethod
    def _flatten_content(content: Any) -> str:
        if isinstance(content, list):
            return "".join(part.get("text", "") for part in content if isinstance(part, dict))
        return "" if content is None else str(content)


__all__ = ["ChatProvider", "extract_json"]
