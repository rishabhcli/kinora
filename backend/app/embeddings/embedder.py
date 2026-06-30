"""The multi-backend :class:`Embedder` abstraction.

An :class:`Embedder` turns images and text into :class:`~app.embeddings.vectors.EmbeddingVector`
values stamped with the embedder's :class:`~app.embeddings.vectors.VectorSpace`.
Every vector an embedder returns is in *its own* space, so the store can refuse
to mix outputs from different backends.

Backends:

* :class:`DashScopeEmbedder` — wraps the round-1
  :class:`app.providers.embeddings.EmbeddingProvider` (``tongyi-embedding-vision-plus``),
  the production path. Image + text in one shared space.
* :class:`OpenAIEmbedder` — adapts an OpenAI-style async embeddings client
  (``text-embedding-3-*``). Text-native; image support is delegated to a caption
  hook if the caller wires one, else it raises.
* :class:`LocalClipEmbedder` — adapts an injected local CLIP-style encoder
  (``encode_image`` / ``encode_text`` callables), for an offline/on-device path.
* :class:`FakeEmbedder` — a deterministic, seeded embedder for tests. Same input
  bytes/text → same vector; no network. Distinct inputs get well-separated
  vectors so k-NN and identity tests are stable.

All four satisfy the :class:`Embedder` runtime-checkable protocol.
"""

from __future__ import annotations

import enum
import hashlib
import math
import struct
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from app.embeddings.models import Modality
from app.embeddings.vectors import EmbeddingVector, VectorSpace


@dataclass(frozen=True, slots=True)
class EmbedRequest:
    """A single item to embed, with its modality and original payload."""

    modality: Modality
    image_bytes: bytes | None = None
    text: str | None = None

    @classmethod
    def for_image(cls, image_bytes: bytes) -> EmbedRequest:
        return cls(modality=Modality.IMAGE, image_bytes=image_bytes)

    @classmethod
    def for_text(cls, text: str) -> EmbedRequest:
        return cls(modality=Modality.TEXT, text=text)


@runtime_checkable
class Embedder(Protocol):
    """An async producer of space-stamped vectors for images and text."""

    @property
    def space(self) -> VectorSpace:
        """The space *all* vectors from this embedder live in."""
        ...

    async def embed_images(self, images: Sequence[bytes]) -> list[EmbeddingVector]:
        """Embed raw image bytes; one vector per image, in :attr:`space`."""
        ...

    async def embed_texts(self, texts: Sequence[str]) -> list[EmbeddingVector]:
        """Embed texts; one vector per text, in :attr:`space`."""
        ...


class BaseEmbedder:
    """Shared plumbing: holds a space and dispatches mixed-modality batches."""

    def __init__(self, space: VectorSpace) -> None:
        self._space = space

    @property
    def space(self) -> VectorSpace:
        return self._space

    async def embed_images(  # pragma: no cover - abstract
        self, images: Sequence[bytes]
    ) -> list[EmbeddingVector]:
        raise NotImplementedError

    async def embed_texts(  # pragma: no cover - abstract
        self, texts: Sequence[str]
    ) -> list[EmbeddingVector]:
        raise NotImplementedError

    async def embed(self, requests: Sequence[EmbedRequest]) -> list[EmbeddingVector]:
        """Embed a heterogeneous batch, preserving input order.

        Images and texts are grouped, embedded in their own (potentially batched)
        calls, then re-interleaved so ``result[i]`` corresponds to ``requests[i]``.
        """
        image_idx: list[int] = []
        text_idx: list[int] = []
        images: list[bytes] = []
        texts: list[str] = []
        for i, req in enumerate(requests):
            if req.modality is Modality.IMAGE:
                if req.image_bytes is None:
                    raise ValueError(f"image request at index {i} has no image_bytes")
                image_idx.append(i)
                images.append(req.image_bytes)
            else:
                if req.text is None:
                    raise ValueError(f"text request at index {i} has no text")
                text_idx.append(i)
                texts.append(req.text)

        out: list[EmbeddingVector | None] = [None] * len(requests)
        if images:
            for slot, vec in zip(image_idx, await self.embed_images(images), strict=True):
                out[slot] = vec
        if texts:
            for slot, vec in zip(text_idx, await self.embed_texts(texts), strict=True):
                out[slot] = vec
        return [v for v in out if v is not None]


# --------------------------------------------------------------------------- #
# DashScope (production)
# --------------------------------------------------------------------------- #
class DashScopeEmbedder(BaseEmbedder):
    """Wraps :class:`app.providers.embeddings.EmbeddingProvider` (tongyi-vision)."""

    def __init__(self, provider: Any, space: VectorSpace) -> None:
        super().__init__(space)
        self._provider = provider

    async def embed_images(self, images: Sequence[bytes]) -> list[EmbeddingVector]:
        raw = await self._provider.embed_images(list(images))
        return [EmbeddingVector.create(self._space, v) for v in raw]

    async def embed_texts(self, texts: Sequence[str]) -> list[EmbeddingVector]:
        raw = await self._provider.embed_texts(list(texts))
        return [EmbeddingVector.create(self._space, v) for v in raw]

    @classmethod
    def from_provider(cls, provider: Any, settings: Any) -> DashScopeEmbedder:
        space = VectorSpace(
            provider="dashscope",
            model=str(getattr(settings, "embed_model_image", "tongyi-embedding-vision-plus")),
            dimension=int(getattr(settings, "embed_dim", 1152)),
            version=1,
        )
        return cls(provider, space)


# --------------------------------------------------------------------------- #
# OpenAI
# --------------------------------------------------------------------------- #
class _ImageUnsupported(enum.Enum):
    SENTINEL = 0


class OpenAIEmbedder(BaseEmbedder):
    """Adapts an OpenAI-style async embeddings client (``text-embedding-3-*``).

    ``client`` must expose ``embeddings.create(model=..., input=[...])`` returning
    an object with ``.data[i].embedding``. OpenAI's text-embedding models are
    text-native; to embed images you must supply an ``image_caption`` hook that
    turns image bytes into a caption string (e.g. a VL model), otherwise image
    embedding raises — surfacing the mixing risk loudly instead of silently
    returning a wrong-space vector.
    """

    def __init__(
        self,
        client: Any,
        space: VectorSpace,
        *,
        image_caption: Callable[[bytes], Awaitable[str]] | None = None,
    ) -> None:
        super().__init__(space)
        self._client = client
        self._caption = image_caption

    async def embed_texts(self, texts: Sequence[str]) -> list[EmbeddingVector]:
        if not texts:
            return []
        rsp = await self._client.embeddings.create(model=self._space.model, input=list(texts))
        return [EmbeddingVector.create(self._space, item.embedding) for item in rsp.data]

    async def embed_images(self, images: Sequence[bytes]) -> list[EmbeddingVector]:
        if not images:
            return []
        if self._caption is None:
            raise NotImplementedError(
                "OpenAIEmbedder cannot embed images without an image_caption hook; "
                "wire one (e.g. a VL captioner) or use DashScope/CLIP for images."
            )
        captions = [await self._caption(img) for img in images]
        return await self.embed_texts(captions)


# --------------------------------------------------------------------------- #
# Local CLIP / on-device
# --------------------------------------------------------------------------- #
class LocalClipEmbedder(BaseEmbedder):
    """Adapts an injected local CLIP-style encoder.

    ``encode_image`` / ``encode_text`` are callables that take a list and return a
    list of float vectors (sync or async). This keeps the heavy torch/CLIP
    dependency out of the import graph — the caller injects whatever it has.
    """

    def __init__(
        self,
        space: VectorSpace,
        *,
        encode_image: Callable[[list[bytes]], Any],
        encode_text: Callable[[list[str]], Any],
    ) -> None:
        super().__init__(space)
        self._encode_image = encode_image
        self._encode_text = encode_text

    async def embed_images(self, images: Sequence[bytes]) -> list[EmbeddingVector]:
        if not images:
            return []
        raw = await _maybe_await(self._encode_image(list(images)))
        return [EmbeddingVector.create(self._space, v) for v in raw]

    async def embed_texts(self, texts: Sequence[str]) -> list[EmbeddingVector]:
        if not texts:
            return []
        raw = await _maybe_await(self._encode_text(list(texts)))
        return [EmbeddingVector.create(self._space, v) for v in raw]


async def _maybe_await(value: Any) -> Any:
    if hasattr(value, "__await__"):
        return await value
    return value


# --------------------------------------------------------------------------- #
# Deterministic fake (tests)
# --------------------------------------------------------------------------- #
class FakeEmbedder(BaseEmbedder):
    """A deterministic, seeded embedder. No network; stable across runs.

    The vector for an input is derived from a SHA-256 of ``(seed, modality, bytes)``
    expanded into ``dimension`` floats via a counter-mode hash stream, then
    L2-normalized. Identical inputs map to identical vectors (cache + dedup
    tests); distinct inputs map to near-orthogonal vectors (k-NN + identity
    tests).

    A small ``anchors`` map lets a test pin specific inputs to specific *base*
    vectors so it can build controlled near-duplicates (e.g. two frames of the
    same character) — see :meth:`with_anchor`.
    """

    def __init__(self, space: VectorSpace, *, seed: int = 0) -> None:
        super().__init__(space)
        self._seed = seed
        self._anchors: dict[bytes, list[float]] = {}

    def with_anchor(self, key: str | bytes, base: Sequence[float]) -> FakeEmbedder:
        """Pin ``key`` to a known base vector (returns self for chaining)."""
        k = key.encode("utf-8") if isinstance(key, str) else key
        if len(base) != self._space.dimension:
            raise ValueError("anchor base length must equal space dimension")
        self._anchors[k] = [float(x) for x in base]
        return self

    def _hash_stream(self, modality: Modality, payload: bytes) -> list[float]:
        if payload in self._anchors:
            return list(self._anchors[payload])
        dim = self._space.dimension
        out: list[float] = []
        prefix = struct.pack(">i", self._seed) + modality.value.encode("ascii") + b":" + payload
        counter = 0
        while len(out) < dim:
            digest = hashlib.sha256(prefix + struct.pack(">I", counter)).digest()
            # 8 doubles per 64-byte digest (8 bytes each) mapped to [-1, 1).
            for off in range(0, len(digest), 8):
                if len(out) >= dim:
                    break
                (val,) = struct.unpack(">Q", digest[off : off + 8])
                out.append((val / float(1 << 64)) * 2.0 - 1.0)
            counter += 1
        return out

    def _vec(self, modality: Modality, payload: bytes) -> EmbeddingVector:
        raw = self._hash_stream(modality, payload)
        # EmbeddingVector.create normalizes; guard the (astronomically unlikely)
        # zero vector so cosine stays well-defined.
        if not any(raw):
            raw[0] = 1.0
        return EmbeddingVector.create(self._space, raw)

    async def embed_images(self, images: Sequence[bytes]) -> list[EmbeddingVector]:
        return [self._vec(Modality.IMAGE, img) for img in images]

    async def embed_texts(self, texts: Sequence[str]) -> list[EmbeddingVector]:
        return [self._vec(Modality.TEXT, t.encode("utf-8")) for t in texts]


def perturb(vec: EmbeddingVector, *, amount: float, axis: int = 0) -> EmbeddingVector:
    """Return a slightly-rotated copy of ``vec`` (test helper for near-duplicates).

    Useful to simulate "the same character, a different frame": a small ``amount``
    keeps cosine high (a MATCH), a large one drops it (a REJECT).
    """
    values = list(vec.values)
    axis %= len(values)
    values[axis] += amount
    # Add a second component so the perturbation isn't degenerate.
    values[(axis + 1) % len(values)] += amount * 0.5
    norm = math.sqrt(math.fsum(x * x for x in values))
    if norm > 0:
        values = [x / norm for x in values]
    return EmbeddingVector(space=vec.space, values=tuple(values))


__all__ = [
    "BaseEmbedder",
    "DashScopeEmbedder",
    "EmbedRequest",
    "Embedder",
    "FakeEmbedder",
    "LocalClipEmbedder",
    "Modality",
    "OpenAIEmbedder",
    "perturb",
]
