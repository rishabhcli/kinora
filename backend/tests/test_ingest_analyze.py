"""Page-analysis unit tests (§9.1 step 2) — rate control + retry, no network.

The VL provider's ``analyze_json`` is monkeypatched, so no DB and no network are
needed: these assert the bounded-concurrency fan-out, the retry-on-transient
behaviour, and that one bad page never fails the batch.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.ingest.analyze import analyze_pages
from app.ingest.pdf_extract import PageExtract
from app.providers import Providers
from tests.test_ingest_support import (
    MemoryBlobStore,
    providers,  # noqa: F401  (pytest fixture)
)


def _page(n: int) -> PageExtract:
    return PageExtract(
        page_number=n,
        image_key=f"pages/{n}.png",
        text=f"page {n} text",
        word_boxes=[],
        word_index_start=0,
        word_index_end=0,
    )


def _store_for(pages: list[PageExtract]) -> MemoryBlobStore:
    store = MemoryBlobStore()
    for p in pages:
        store.put_bytes(p.image_key, b"\x89PNGfake")
    return store


async def test_analyze_pages_orders_and_tags_page_numbers(
    providers: Providers,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pages = [_page(i) for i in range(1, 6)]
    store = _store_for(pages)

    async def fake(images: list[Any], prompt: str, **kw: Any) -> dict[str, Any]:
        # Echo a summary so we can confirm ordering is preserved.
        return {"summary": prompt.splitlines()[-1]}

    monkeypatch.setattr(providers.vl, "analyze_json", fake)
    analyses = await analyze_pages(pages, providers=providers, blob_store=store)

    assert [a.page_number for a in analyses] == [1, 2, 3, 4, 5]
    assert all(a.summary for a in analyses)


async def test_analyze_pages_retries_transient_then_succeeds(
    providers: Providers,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pages = [_page(1)]
    store = _store_for(pages)
    attempts = {"n": 0}

    async def flaky(images: list[Any], prompt: str, **kw: Any) -> dict[str, Any]:
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise RuntimeError("429 Throttling.RateQuota")
        return {"summary": "recovered"}

    monkeypatch.setattr(providers.vl, "analyze_json", flaky)
    analyses = await analyze_pages(
        pages,
        providers=providers,
        blob_store=store,
        max_attempts=4,
        backoff_base_s=0.0,  # no real waiting in the test
    )

    assert attempts["n"] == 3
    assert analyses[0].summary == "recovered"


async def test_analyze_pages_one_bad_page_does_not_fail_batch(
    providers: Providers,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pages = [_page(1), _page(2), _page(3)]
    store = _store_for(pages)

    async def maybe_fail(images: list[Any], prompt: str, **kw: Any) -> dict[str, Any]:
        if "page 2" in prompt:
            raise ValueError("permanent parse failure")  # non-transient: no retry
        return {"summary": "ok"}

    monkeypatch.setattr(providers.vl, "analyze_json", maybe_fail)
    analyses = await analyze_pages(pages, providers=providers, blob_store=store)

    # Page 2 yields an empty analysis; the other two are fine.
    assert len(analyses) == 3
    assert analyses[1].summary == ""  # the failed page is empty, not missing


async def test_analyze_pages_rate_limit_does_not_deadlock(
    providers: Providers,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pages = [_page(i) for i in range(1, 11)]
    store = _store_for(pages)

    async def fast(images: list[Any], prompt: str, **kw: Any) -> dict[str, Any]:
        return {"summary": "x"}

    monkeypatch.setattr(providers.vl, "analyze_json", fast)
    # A real (small) rate cap + burst — must still complete every page.
    analyses = await analyze_pages(
        pages,
        providers=providers,
        blob_store=store,
        concurrency=4,
        rate_per_s=1000.0,
        rate_burst=4,
    )

    assert len(analyses) == 10


async def test_analyze_pages_empty_returns_empty(
    providers: Providers,  # noqa: F811
) -> None:
    assert await analyze_pages([], providers=providers, blob_store=MemoryBlobStore()) == []
