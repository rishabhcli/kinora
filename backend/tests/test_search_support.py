"""Shared fixtures/helpers for the search tests (no network, deterministic)."""

from __future__ import annotations

import hashlib

from app.search.documents import DocKind, SearchDocument

_EMBED_DIM = 1152


class FakeEmbedder:
    """Deterministic hash-bucketed embedder (same shape as the conftest one).

    Maps text/image bytes to a sparse unit vector by hashing into a fixed bucket,
    so identical text embeds identically and the cosine arm is reproducible —
    never a live DashScope call (zero credits).
    """

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        return [self._vector(t.encode("utf-8")) for t in texts]

    async def embed_images(self, images: list[bytes]) -> list[list[float]]:
        return [self._vector(b) for b in images]

    @staticmethod
    def _vector(data: bytes) -> list[float]:
        digest = hashlib.sha1(data).digest()
        # Light up three buckets so different-but-related texts share some mass.
        vec = [0.0] * _EMBED_DIM
        for i in range(3):
            axis = int.from_bytes(digest[i * 4 : i * 4 + 4], "big") % _EMBED_DIM
            vec[axis] += 1.0
        norm = sum(x * x for x in vec) ** 0.5
        return [x / norm for x in vec] if norm else vec


def sample_docs() -> list[SearchDocument]:
    """A tiny library across every doc kind, for engine behaviour tests."""
    return [
        SearchDocument(
            doc_id="book:b1",
            kind=DocKind.BOOK,
            ref_id="b1",
            book_id="b1",
            title="The Snow Queen",
            body="A frozen fairy tale by Hans Christian Andersen.",
            keywords=["Andersen"],
            facets={"status": "ready"},
            numbers={"num_pages": 120.0},
        ),
        SearchDocument(
            doc_id="page:p1",
            kind=DocKind.PAGE,
            ref_id="p1",
            book_id="b1",
            title="Page 3",
            body="Gerda walked through the frozen forest searching for Kai.",
            facets={},
            numbers={"page_number": 3.0},
        ),
        SearchDocument(
            doc_id="beat:bt1",
            kind=DocKind.BEAT,
            ref_id="bt1",
            book_id="b1",
            title="Gerda enters the palace",
            body="Gerda runs through the ice palace as frost spreads across the walls.",
            keywords=["char_gerda", "loc_palace"],
            facets={"scene_id": "sc1"},
            numbers={"beat_index": 7.0},
        ),
        SearchDocument(
            doc_id="entity:b1:char_gerda",
            kind=DocKind.ENTITY,
            ref_id="char_gerda",
            book_id="b1",
            title="Gerda",
            body="A brave young girl with red boots searching for her friend.",
            keywords=["the girl", "Little Gerda"],
            facets={"entity_type": "character"},
            numbers={"version": 2.0},
        ),
        SearchDocument(
            doc_id="shot:s1",
            kind=DocKind.SHOT,
            ref_id="s1",
            book_id="b1",
            title="Wide shot of the frozen palace gates",
            body="A slow push-in on the glittering ice gates under a pale sun.",
            keywords=["char_gerda@v2", "loc_palace@v1"],
            facets={"status": "accepted", "render_mode": "reference_to_video"},
            numbers={"duration_s": 5.0, "score": 0.9},
        ),
        SearchDocument(
            doc_id="shot:s2",
            kind=DocKind.SHOT,
            ref_id="s2",
            book_id="b2",  # a DIFFERENT book (for scope tests)
            title="A summer meadow",
            body="Warm golden light over a bright green meadow full of flowers.",
            facets={"status": "accepted", "render_mode": "text_to_video"},
            numbers={"duration_s": 8.0, "score": 0.7},
        ),
    ]
