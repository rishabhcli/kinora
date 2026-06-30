"""Multi-backend embedder: determinism, space stamping, mixed-batch, adapters."""

from __future__ import annotations

import pytest

from app.embeddings.embedder import (
    DashScopeEmbedder,
    Embedder,
    EmbedRequest,
    FakeEmbedder,
    LocalClipEmbedder,
    OpenAIEmbedder,
    perturb,
)
from app.embeddings.models import Modality
from app.embeddings.vectors import VectorSpace

SPACE = VectorSpace(provider="dashscope", model="tongyi", dimension=16, version=1)


def make_fake() -> FakeEmbedder:
    return FakeEmbedder(SPACE, seed=7)


async def test_fake_is_deterministic_and_unit() -> None:
    a = make_fake()
    b = make_fake()
    [va] = await a.embed_texts(["elsa"])
    [vb] = await b.embed_texts(["elsa"])
    assert va.values == vb.values  # same seed + input -> identical
    assert va.is_unit()
    assert va.space == SPACE


async def test_fake_distinct_inputs_well_separated() -> None:
    e = make_fake()
    [elsa] = await e.embed_texts(["elsa"])
    [anna] = await e.embed_texts(["anna"])
    # Random hash vectors in 16-d should be far from collinear.
    assert abs(elsa.cosine(anna)) < 0.6


async def test_fake_satisfies_embedder_protocol() -> None:
    assert isinstance(make_fake(), Embedder)


async def test_image_vs_text_modality_differs() -> None:
    e = make_fake()
    [as_text] = await e.embed_texts(["abc"])
    [as_img] = await e.embed_images([b"abc"])
    # Same payload bytes but different modality salt -> different vectors.
    assert as_text.values != as_img.values


async def test_anchor_and_perturb_near_duplicate() -> None:
    e = make_fake()
    base = [1.0] + [0.0] * 15
    e.with_anchor("frame_a", base)
    [va] = await e.embed_images([b"frame_a"])
    near = perturb(va, amount=0.02)
    far = perturb(va, amount=2.0)
    assert va.cosine(near) > 0.99  # tiny perturbation -> still the same
    assert va.cosine(far) < va.cosine(near)


async def test_mixed_batch_preserves_order() -> None:
    e = make_fake()
    reqs = [
        EmbedRequest.for_text("a"),
        EmbedRequest.for_image(b"img"),
        EmbedRequest.for_text("b"),
    ]
    out = await e.embed(reqs)
    assert len(out) == 3
    [a] = await e.embed_texts(["a"])
    [img] = await e.embed_images([b"img"])
    [b] = await e.embed_texts(["b"])
    assert out[0].values == a.values
    assert out[1].values == img.values
    assert out[2].values == b.values


async def test_dashscope_adapter_stamps_space() -> None:
    class _Provider:
        async def embed_images(self, images: list[bytes]) -> list[list[float]]:
            return [[1.0] + [0.0] * 15 for _ in images]

        async def embed_texts(self, texts: list[str]) -> list[list[float]]:
            return [[0.0, 1.0] + [0.0] * 14 for _ in texts]

    class _Settings:
        embed_model_image = "tongyi"
        embed_dim = 16

    e = DashScopeEmbedder.from_provider(_Provider(), _Settings())
    assert e.space.provider == "dashscope"
    assert e.space.dimension == 16
    [v] = await e.embed_images([b"x"])
    assert v.space == e.space
    assert v.is_unit()


async def test_openai_adapter_text_and_image_hook() -> None:
    class _Resp:
        def __init__(self, vecs: list[list[float]]) -> None:
            self.data = [type("D", (), {"embedding": v})() for v in vecs]

    class _Embeddings:
        async def create(self, *, model: str, input: list[str]) -> _Resp:
            return _Resp([[float(len(t))] + [0.0] * 15 for t in input])

    class _Client:
        embeddings = _Embeddings()

    space = VectorSpace(provider="openai", model="te-3", dimension=16, version=1)

    # Without a caption hook, image embedding must fail loudly (no silent mixing).
    e = OpenAIEmbedder(_Client(), space)
    with pytest.raises(NotImplementedError):
        await e.embed_images([b"img"])

    async def caption(_: bytes) -> str:
        return "a caption"

    e2 = OpenAIEmbedder(_Client(), space, image_caption=caption)
    [v] = await e2.embed_images([b"img"])
    assert v.space == space


async def test_local_clip_adapter_sync_and_async_encoders() -> None:
    space = VectorSpace(provider="clip", model="vit-b32", dimension=16, version=1)

    def enc_img(imgs: list[bytes]) -> list[list[float]]:
        return [[1.0] + [0.0] * 15 for _ in imgs]

    async def enc_txt(txts: list[str]) -> list[list[float]]:
        return [[0.0, 1.0] + [0.0] * 14 for _ in txts]

    e = LocalClipEmbedder(space, encode_image=enc_img, encode_text=enc_txt)
    [vi] = await e.embed_images([b"x"])
    [vt] = await e.embed_texts(["y"])
    assert vi.space == space and vt.space == space


async def test_embed_request_validation() -> None:
    e = make_fake()
    bad = EmbedRequest(modality=Modality.IMAGE, image_bytes=None)
    with pytest.raises(ValueError):
        await e.embed([bad])
