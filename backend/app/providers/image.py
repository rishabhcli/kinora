"""Image generation + instruction-edit (qwen-image-2.0-pro / qwen-image-edit-max).

Generation powers identity-lock keyframes and speculative keyframes (§9.1); edit
powers the Director's surgical "make the coat red" change (§9.3 instruction_edit
on stills). Both go through the native multimodal-generation service via the
``dashscope`` SDK and return raw image bytes (downloaded from the signed URL the
API returns).
"""

from __future__ import annotations

import functools
from typing import Any

from .base import ProviderClient, data_uri
from .base import sdk_get as _get
from .errors import ResponseParseError
from .types import Usage
from .vl import _sniff_mime

ImageInput = bytes | str


def _image_urls_from_response(rsp: Any) -> list[str]:
    output = _get(rsp, "output")
    choices = _get(output, "choices")
    if not choices:
        raise ResponseParseError("image response had no choices")
    content = _get(_get(choices[0], "message"), "content") or []
    urls: list[str] = []
    for item in content:
        url = _get(item, "image") or _get(item, "url")
        if url:
            urls.append(url)
    if not urls:
        raise ResponseParseError("image response contained no image url")
    return urls


def _image_count(rsp: Any, fallback: int) -> int:
    usage = _get(rsp, "usage")
    count = _get(usage, "image_count")
    return int(count) if count else fallback


def _as_input_url(image: ImageInput) -> str:
    return data_uri(image, _sniff_mime(image)) if isinstance(image, bytes) else image


class ImageProvider:
    """Async image generation + edit client."""

    def __init__(self, client: ProviderClient) -> None:
        self._client = client
        self._settings = client.settings

    async def generate(
        self,
        prompt: str,
        *,
        size: str = "1328*1328",  # qwen-image-plus allowed size; callers override
        n: int = 1,
        negative_prompt: str | None = None,
        reference_images: list[ImageInput] | None = None,
        seed: int | None = None,
        model: str | None = None,
        timeout: float | None = None,
    ) -> list[bytes]:
        """Generate ``n`` images; returns raw bytes for each."""
        model = model or self._settings.image_model
        images: list[bytes] = []
        # qwen-image returns one image per call; loop to honour n deterministically
        # (offsetting the seed so multiple draws differ).
        for i in range(max(n, 1)):
            urls = await self._generate_once(
                prompt,
                size=size,
                negative_prompt=negative_prompt,
                reference_images=reference_images,
                seed=None if seed is None else seed + i,
                model=model,
                timeout=timeout,
            )
            for url in urls:
                images.append(await self._client.download(url, op="image"))
                if len(images) >= n:
                    return images
        return images

    async def _generate_once(
        self,
        prompt: str,
        *,
        size: str,
        negative_prompt: str | None,
        reference_images: list[ImageInput] | None,
        seed: int | None,
        model: str,
        timeout: float | None,
    ) -> list[str]:
        from dashscope import MultiModalConversation

        content: list[dict[str, Any]] = []
        for ref in reference_images or []:
            content.append({"image": _as_input_url(ref)})
        content.append({"text": prompt})
        sdk_kwargs: dict[str, Any] = {
            "api_key": self._client.api_key,
            "model": model,
            "messages": [{"role": "user", "content": content}],
            "result_format": "message",
            "watermark": False,
            "prompt_extend": False,
            "size": size,
        }
        if negative_prompt is not None:
            sdk_kwargs["negative_prompt"] = negative_prompt
        if seed is not None:
            sdk_kwargs["seed"] = seed

        call = functools.partial(MultiModalConversation.call, **sdk_kwargs)
        rsp = await self._client.call_sdk(call, op="image", model=model, timeout=timeout)
        urls = _image_urls_from_response(rsp)
        self._client.record_usage(
            Usage(
                model=model,
                operation="image",
                images=_image_count(rsp, len(urls)),
                request_id=_get(rsp, "request_id"),
            )
        )
        return urls

    async def edit(
        self,
        image: bytes,
        instruction: str,
        *,
        mask: bytes | None = None,
        model: str | None = None,
        timeout: float | None = None,
    ) -> bytes:
        """Apply an instruction edit to ``image`` and return the edited bytes."""
        from dashscope import MultiModalConversation

        model = model or self._settings.image_edit_model
        content: list[dict[str, Any]] = [{"image": _as_input_url(image)}]
        if mask is not None:
            content.append({"image": _as_input_url(mask)})
        content.append({"text": instruction})
        call = functools.partial(
            MultiModalConversation.call,
            api_key=self._client.api_key,
            model=model,
            messages=[{"role": "user", "content": content}],
            result_format="message",
            watermark=False,
        )
        rsp = await self._client.call_sdk(call, op="image_edit", model=model, timeout=timeout)
        urls = _image_urls_from_response(rsp)
        self._client.record_usage(
            Usage(
                model=model,
                operation="image",
                images=_image_count(rsp, 1),
                request_id=_get(rsp, "request_id"),
            )
        )
        return await self._client.download(urls[0], op="image_edit")


__all__ = ["ImageProvider"]
