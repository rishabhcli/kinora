"""Unit tests for the integrations primitives (no infra, no network).

Covers the normalized format + content hashing, the document renderer (real
PyMuPDF HTML→PDF), the HTTP error mapping + fake client, the backoff schedule +
retry classification, the clock, and the token sealer.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.integrations.backoff import BackoffPolicy, is_retryable, retry_after_of
from app.integrations.clock import FakeClock
from app.integrations.crypto import TokenSealer
from app.integrations.document import render_html, render_pdf
from app.integrations.errors import (
    AuthExpired,
    ConnectorError,
    PermanentError,
    RateLimited,
    TransientError,
)
from app.integrations.http import FakeHttpClient, HttpResponse
from app.integrations.models import (
    BlockKind,
    NormalizedBlock,
    NormalizedDocument,
    SourceItem,
)


# --------------------------------------------------------------------------- #
# Normalized format + hashing
# --------------------------------------------------------------------------- #
def _doc(title: str = "T", text: str = "hello world foo bar") -> NormalizedDocument:
    return NormalizedDocument(
        title=title, author="A", blocks=(NormalizedBlock(text=text),)
    )


def test_document_word_count_and_emptiness() -> None:
    assert _doc(text="a b c").word_count() == 3
    empty = NormalizedDocument(title="x", blocks=())
    assert empty.is_empty()
    assert not _doc().is_empty()


def test_content_hash_is_stable_and_change_sensitive() -> None:
    a = SourceItem(source_id="s1", document=_doc(text="same text here"))
    b = SourceItem(source_id="s1", document=_doc(text="same text here"))
    c = SourceItem(source_id="s1", document=_doc(text="different text"))
    assert a.content_hash == b.content_hash
    assert a.content_hash != c.content_hash


# --------------------------------------------------------------------------- #
# Document renderer
# --------------------------------------------------------------------------- #
def test_render_html_escapes_and_includes_blocks() -> None:
    doc = NormalizedDocument(
        title="Title <b>x</b>",
        author="Me & You",
        blocks=(
            NormalizedBlock(kind=BlockKind.HEADING, text="Chapter"),
            NormalizedBlock(kind=BlockKind.QUOTE, text="quoted", cite="p.1"),
        ),
    )
    html = render_html(doc)
    assert "&lt;b&gt;x&lt;/b&gt;" in html  # title escaped
    assert "Me &amp; You" in html
    assert "<blockquote>" in html and "p.1" in html


def test_render_pdf_produces_valid_pdf_with_text() -> None:
    pdf = render_pdf(_doc(text="The quick brown fox jumps over the lazy dog."))
    assert pdf[:5] == b"%PDF-"
    assert len(pdf) > 1000
    import fitz

    d = fitz.open(stream=pdf, filetype="pdf")
    try:
        assert d.page_count >= 1
        assert "quick brown fox" in d[0].get_text()
    finally:
        d.close()


def test_render_pdf_rejects_empty_document() -> None:
    with pytest.raises(ConnectorError):
        render_pdf(NormalizedDocument(title="x", blocks=()))


# --------------------------------------------------------------------------- #
# HTTP error mapping + fake client
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("status", "exc"),
    [
        (429, RateLimited),
        (401, AuthExpired),
        (403, AuthExpired),
        (500, TransientError),
        (503, TransientError),
        (400, PermanentError),
        (404, PermanentError),
    ],
)
def test_raise_for_status_maps_codes(status: int, exc: type[Exception]) -> None:
    with pytest.raises(exc):
        HttpResponse(status=status).raise_for_status()


def test_raise_for_status_passes_2xx_3xx() -> None:
    assert HttpResponse(status=200).raise_for_status().status == 200
    assert HttpResponse(status=302).raise_for_status().status == 302


def test_rate_limited_parses_retry_after() -> None:
    resp = HttpResponse(status=429, headers={"retry-after": "12"})
    with pytest.raises(RateLimited) as ei:
        resp.raise_for_status()
    assert ei.value.retry_after_s == 12.0


@pytest.mark.asyncio
async def test_fake_http_client_routes_and_records() -> None:
    fake = FakeHttpClient().json_response("GET", "/export", {"ok": True})
    resp = await fake.request("GET", "https://x/export?a=1")
    assert resp.json() == {"ok": True}
    assert fake.requests[0].method == "GET"


@pytest.mark.asyncio
async def test_fake_http_client_paginates_via_list() -> None:
    fake = FakeHttpClient().add(
        "GET",
        "/page",
        [HttpResponse(status=200, content=b"1"), HttpResponse(status=200, content=b"2")],
    )
    assert (await fake.request("GET", "https://x/page")).text == "1"
    assert (await fake.request("GET", "https://x/page")).text == "2"


@pytest.mark.asyncio
async def test_fake_http_client_unmatched_raises() -> None:
    fake = FakeHttpClient()
    with pytest.raises(AssertionError):
        await fake.request("GET", "https://x/nope")


# --------------------------------------------------------------------------- #
# Backoff
# --------------------------------------------------------------------------- #
def test_backoff_base_delay_grows_and_caps() -> None:
    policy = BackoffPolicy(base_s=1.0, factor=2.0, cap_s=10.0, jitter=0.0)
    assert policy.base_delay(0) == 1.0
    assert policy.base_delay(1) == 2.0
    assert policy.base_delay(2) == 4.0
    assert policy.base_delay(10) == 10.0  # capped


def test_backoff_jitter_is_within_band() -> None:
    policy = BackoffPolicy(base_s=4.0, factor=2.0, cap_s=100.0, jitter=0.25)
    lo = policy.delay(0, rand=lambda: 0.0)
    hi = policy.delay(0, rand=lambda: 1.0)
    assert lo == pytest.approx(3.0)  # 4 * (1-0.25)
    assert hi == pytest.approx(5.0)  # 4 * (1+0.25)


def test_backoff_honours_retry_after() -> None:
    policy = BackoffPolicy(base_s=1.0, cap_s=100.0)
    assert policy.delay(0, retry_after_s=42.0) == 42.0


def test_retry_classification() -> None:
    assert is_retryable(TransientError("x"))
    assert is_retryable(RateLimited("x"))
    assert not is_retryable(PermanentError("x"))
    assert not is_retryable(AuthExpired("x"))
    assert not is_retryable(ValueError("x"))
    assert retry_after_of(RateLimited("x", retry_after_s=5)) == 5
    assert retry_after_of(TransientError("x")) is None


# --------------------------------------------------------------------------- #
# Clock
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_fake_clock_records_and_advances() -> None:
    clock = FakeClock(start=datetime(2026, 1, 1, tzinfo=UTC))
    await clock.sleep(5)
    await clock.sleep(3)
    assert clock.slept == [5, 3]
    assert clock.now() == datetime(2026, 1, 1, 0, 0, 8, tzinfo=UTC)


# --------------------------------------------------------------------------- #
# Token sealer
# --------------------------------------------------------------------------- #
def test_token_sealer_roundtrip_fallback() -> None:
    sealer = TokenSealer(key=None)
    blob = {"access_token": "abc", "refresh_token": "xyz", "n": "1"}
    sealed = sealer.seal(blob)
    assert sealed.startswith("v0:")
    assert "abc" not in sealed  # not plaintext
    assert sealer.unseal(sealed) == blob
    assert not sealer.is_strong


def test_token_sealer_roundtrip_strong() -> None:
    sealer = TokenSealer(key="a-real-secret-key")
    assert sealer.is_strong
    blob = {"access_token": "secret-token"}
    sealed = sealer.seal(blob)
    assert sealed.startswith("f1:")
    assert "secret-token" not in sealed
    assert sealer.unseal(sealed)["access_token"] == "secret-token"


def test_token_sealer_rejects_garbage() -> None:
    with pytest.raises(ValueError):
        TokenSealer(key=None).unseal("not-a-valid-blob")
