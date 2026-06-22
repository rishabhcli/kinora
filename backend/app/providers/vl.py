"""Vision-language analysis (qwen-vl-max) over the compatible-mode endpoint.

Two jobs (§9.1, §9.5): reading book pages (text + layout + illustrations) during
ingest, and scoring rendered clips/frames against the canon for the Critic.
Local image bytes are base64-encoded into ``data:`` URIs; remote refs pass
through as URLs.
"""

from __future__ import annotations

from typing import Any

from .base import ProviderClient, data_uri
from .chat import JsonValue, extract_json
from .errors import ResponseParseError
from .types import Usage

ImageInput = bytes | str

_JSON_REMINDER = (
    "Your previous reply was not valid JSON. Return ONLY a single valid JSON "
    "value with no prose and no markdown code fences."
)


def _sniff_mime(raw: bytes) -> str:
    if raw[:8].startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if raw[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if raw[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    return "image/png"


def _image_url_part(image: ImageInput) -> dict[str, Any]:
    url = data_uri(image, _sniff_mime(image)) if isinstance(image, bytes) else image
    return {"type": "image_url", "image_url": {"url": url}}


class VLProvider:
    """Async multimodal analysis client."""

    def __init__(self, client: ProviderClient) -> None:
        self._client = client
        self._settings = client.settings

    async def analyze(
        self,
        images: list[ImageInput],
        prompt: str,
        *,
        model: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        timeout: float | None = None,
    ) -> str:
        """Describe/score one or more images against ``prompt``; returns text."""
        model = model or self._settings.vl_model
        content: list[dict[str, Any]] = [_image_url_part(img) for img in images]
        content.append({"type": "text", "text": prompt})
        messages = [{"role": "user", "content": content}]
        return await self._complete(messages, model, max_tokens, temperature, timeout)

    async def analyze_json(
        self,
        images: list[ImageInput],
        prompt: str,
        *,
        model: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        timeout: float | None = None,
    ) -> JsonValue:
        """Like :meth:`analyze` but parses JSON, repairing one parse failure."""
        model = model or self._settings.vl_model
        content: list[dict[str, Any]] = [_image_url_part(img) for img in images]
        content.append({"type": "text", "text": prompt})
        messages: list[dict[str, Any]] = [{"role": "user", "content": content}]
        json_format = {"type": "json_object"}
        text = await self._complete(
            messages, model, max_tokens, temperature, timeout, response_format=json_format
        )
        try:
            return extract_json(text)
        except ResponseParseError:
            messages.append({"role": "assistant", "content": text})
            messages.append({"role": "user", "content": _JSON_REMINDER})
            retry = await self._complete(
                messages, model, max_tokens, temperature, timeout, response_format=json_format
            )
            return extract_json(retry)

    async def _complete(
        self,
        messages: list[dict[str, Any]],
        model: str,
        max_tokens: int | None,
        temperature: float | None,
        timeout: float | None,
        *,
        response_format: dict[str, Any] | None = None,
    ) -> str:
        payload: dict[str, Any] = {"model": model, "messages": messages}
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if temperature is not None:
            payload["temperature"] = temperature
        if response_format is not None:
            payload["response_format"] = response_format

        body = await self._client.request_json(
            "POST",
            f"{self._client.compat_base}/chat/completions",
            op="vl",
            model=model,
            json=payload,
            timeout=timeout,
        )
        choices = body.get("choices") or []
        if not choices:
            raise ResponseParseError("VL response had no choices", request_id=body.get("id"))
        content = (choices[0].get("message") or {}).get("content")
        if isinstance(content, list):
            text = "".join(p.get("text", "") for p in content if isinstance(p, dict))
        else:
            text = content or ""
        usage = body.get("usage") or {}
        self._client.record_usage(
            Usage(
                model=model,
                operation="vl",
                input_tokens=int(usage.get("prompt_tokens", 0)),
                output_tokens=int(usage.get("completion_tokens", 0)),
                request_id=body.get("id"),
            )
        )
        return text


__all__ = ["VLProvider"]
