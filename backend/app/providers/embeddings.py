"""Real multimodal embeddings (image + text) in one shared space.

The memory layer needs embeddings for two jobs (§9.5, §8): the Critic's
character-consistency score (CCS = cosine of a rendered character crop vs the
locked reference image) and episodic "what worked before" retrieval over shots.
Both are image-vs-image, so the ideal model embeds images **and** text into a
single space.

``tongyi-embedding-vision-plus`` (DashScope native multimodal-embedding) does
exactly that — verified live: images and text both embed to **1152-dim** unit
vectors in one shared space, with a configurable dimension. We use the same
model for text so canon text and shot/appearance vectors are directly
comparable. Vectors are L2-normalized here so ``cosine == dot``.

``EMBED_DIM`` is the canonical pgvector dimension **D** for
``entities.embedding`` / ``shots.embedding``.
"""

from __future__ import annotations

import functools
import math
from typing import Any

from .base import ProviderClient, data_uri
from .base import sdk_get as _get
from .errors import ResponseParseError
from .types import Usage
from .vl import _sniff_mime

#: Canonical embedding dimension D for the pgvector columns. This must match the
#: ``embed_dim`` Settings default and the DB ``Vector(D)`` column definitions.
EMBED_DIM = 1152

#: Conservative max contents per multimodal-embedding request (batched by index).
_MAX_BATCH = 8


def cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity. For unit-normalized vectors this equals the dot product."""
    if len(a) != len(b):
        raise ValueError(f"vector length mismatch: {len(a)} != {len(b)}")
    dot = math.fsum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(math.fsum(x * x for x in a))
    nb = math.sqrt(math.fsum(x * x for x in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def _normalize(vec: list[float]) -> list[float]:
    """Return the unit-normalized vector (so cosine == dot downstream)."""
    norm = math.sqrt(math.fsum(x * x for x in vec))
    if norm == 0.0:
        return vec
    return [x / norm for x in vec]


def _chunks(items: list[Any], size: int) -> list[list[Any]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


class EmbeddingProvider:
    """Async multimodal embedding client (shared image+text space)."""

    def __init__(self, client: ProviderClient) -> None:
        self._client = client
        self._settings = client.settings
        self._dim = self._settings.embed_dim

    async def embed_images(self, images: list[bytes]) -> list[list[float]]:
        """Embed raw image bytes; returns one unit-normalized vector per image."""
        if not images:
            return []
        from dashscope import MultiModalEmbeddingItemImage as ItemImage

        model = self._settings.embed_model_image
        items = [ItemImage(image=data_uri(img, _sniff_mime(img)), factor=1.0) for img in images]
        return await self._run(model, items, n_images=len(images), n_texts=0)

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        """Embed texts into the same space; returns one unit-normalized vector each."""
        if not texts:
            return []
        from dashscope import MultiModalEmbeddingItemText as ItemText

        model = self._settings.embed_model_text
        items = [ItemText(text=t, factor=1.0) for t in texts]
        return await self._run(model, items, n_images=0, n_texts=len(texts))

    async def _run(
        self,
        model: str,
        items: list[Any],
        *,
        n_images: int,
        n_texts: int,
    ) -> list[list[float]]:
        from dashscope import MultiModalEmbedding

        out: list[list[float]] = []
        # Attribute the per-content spend proportionally across batches.
        per_item_images = n_images / max(len(items), 1)
        per_item_texts = n_texts / max(len(items), 1)
        for chunk in _chunks(items, _MAX_BATCH):
            call = functools.partial(
                MultiModalEmbedding.call,
                model=model,
                input=chunk,
                api_key=self._client.api_key,
                dimension=self._dim,
            )
            rsp = await self._client.call_sdk(call, op="embedding", model=model)
            out.extend(self._parse(rsp, expected=len(chunk)))
            self._record_usage(
                rsp,
                model,
                images=round(per_item_images * len(chunk)),
                texts=round(per_item_texts * len(chunk)),
            )
        return out

    def _parse(self, rsp: Any, *, expected: int) -> list[list[float]]:
        raw = _get(_get(rsp, "output"), "embeddings")
        if not raw:
            raise ResponseParseError("embedding response had no embeddings")
        # The API returns one item per content tagged with its input index;
        # sort by index so output order matches input order.
        ordered = sorted(raw, key=lambda e: int(_get(e, "index") or 0))
        vectors = [_normalize([float(x) for x in (_get(e, "embedding") or [])]) for e in ordered]
        if len(vectors) != expected:
            raise ResponseParseError(
                f"embedding count mismatch: got {len(vectors)}, expected {expected}",
            )
        return vectors

    def _record_usage(self, rsp: Any, model: str, *, images: int, texts: int) -> None:
        usage = _get(rsp, "usage") or {}
        input_tokens = int(_get(usage, "input_tokens") or 0)
        self._client.record_usage(
            Usage(
                model=model,
                operation="embedding",
                input_tokens=input_tokens,
                images=images,
                request_id=_get(rsp, "request_id"),
            )
        )


__all__ = ["EMBED_DIM", "EmbeddingProvider", "cosine"]
