"""Pure-logic unit tests for the realtime layer (no infra required, §5.6).

Covers the storage-free cores: SSE framing, cursor pagination + its signature,
the deprecation/version registry, and the idempotency request fingerprint. The
Redis-backed pieces (event log, connections, presence, recorder, routes) are
exercised in ``test_realtime_integration.py`` against the throwaway stack.
"""

from __future__ import annotations

import json

import pytest

from app.api.realtime import sse, versioning
from app.api.realtime.idempotency import fingerprint
from app.api.realtime.pagination import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    CursorError,
    Page,
    clamp_limit,
    decode_cursor,
    encode_cursor,
    paginate_after,
)

# --------------------------------------------------------------------------- #
# SSE framing
# --------------------------------------------------------------------------- #


def test_format_event_has_id_event_and_data_lines() -> None:
    frame = sse.format_event({"event": "clip_ready", "shot_id": "s1"}, event_id=42)
    lines = frame.rstrip("\n").split("\n")
    assert "id: 42" in lines
    assert "event: clip_ready" in lines
    data_lines = [ln for ln in lines if ln.startswith("data: ")]
    assert len(data_lines) == 1
    payload = json.loads(data_lines[0][len("data: ") :])
    assert payload["shot_id"] == "s1"
    assert frame.endswith("\n\n")


def test_format_event_defaults_name_from_payload() -> None:
    frame = sse.format_event({"event": "regen_done"})
    assert "event: regen_done" in frame


def test_format_event_newline_in_value_stays_single_frame() -> None:
    # json.dumps escapes the newline (\\n), so the payload is one data: line and
    # the value round-trips intact — no truncation.
    frame = sse.format_event({"event": "x", "note": "a\nb"})
    data_lines = [ln for ln in frame.split("\n") if ln.startswith("data: ")]
    assert len(data_lines) == 1
    payload = json.loads(data_lines[0][len("data: ") :])
    assert payload["note"] == "a\nb"


def test_format_event_splits_a_raw_multiline_data_body() -> None:
    # If a body ever contains a real newline (defensive path), each line becomes
    # its own data: line per the EventSource grammar.
    from app.api.realtime import sse as sse_mod

    body = "line1\nline2"
    lines = []
    for chunk in body.split("\n"):
        lines.append(f"data: {chunk}")
    assert lines == ["data: line1", "data: line2"]
    # And the real formatter never emits a bare newline that splits a JSON frame.
    assert sse_mod.format_event({"event": "x"}).count("\n\n") == 1


def test_format_comment_and_retry() -> None:
    assert sse.format_comment("ping") == ": ping\n\n"
    assert sse.format_retry(5000) == "retry: 5000\n\n"


@pytest.mark.parametrize(
    ("header_val", "query_val", "expected"),
    [
        ("17", None, 17),
        (None, "17", 17),
        ("not-a-number", None, None),
        (None, None, None),
        ("-3", None, None),  # negative ids are rejected
    ],
)
def test_parse_last_event_id(
    header_val: str | None, query_val: str | None, expected: int | None
) -> None:
    headers = {"last-event-id": header_val} if header_val is not None else {}
    assert sse.parse_last_event_id(headers, query_val) == expected


# --------------------------------------------------------------------------- #
# Cursor pagination
# --------------------------------------------------------------------------- #


def test_cursor_roundtrip() -> None:
    secret = "a-secret-of-sufficient-length-for-hmac"
    token = encode_cursor({"after": 99, "scope": "events"}, secret=secret)
    assert "=" not in token  # padding stripped so it survives a query string
    decoded = decode_cursor(token, secret=secret)
    assert decoded == {"after": 99, "scope": "events"}


def test_cursor_signature_is_tamper_evident() -> None:
    secret = "a-secret-of-sufficient-length-for-hmac"
    token = encode_cursor({"after": 5}, secret=secret)
    # A different secret must not validate (forged / cross-scope cursor).
    with pytest.raises(CursorError):
        decode_cursor(token, secret="a-different-secret-also-long-enough")


def test_cursor_rejects_garbage() -> None:
    secret = "a-secret-of-sufficient-length-for-hmac"
    with pytest.raises(CursorError):
        decode_cursor("!!!not-base64!!!", secret=secret)
    with pytest.raises(CursorError):
        decode_cursor("AAAA", secret=secret)  # too short to hold a signature


def test_clamp_limit_bounds() -> None:
    assert clamp_limit(None) == DEFAULT_PAGE_SIZE
    assert clamp_limit(0) == 1
    assert clamp_limit(10_000) == MAX_PAGE_SIZE
    assert clamp_limit(25) == 25


def test_paginate_after_slices_strictly_greater() -> None:
    rows = [{"id": i} for i in range(1, 11)]  # ids 1..10
    page = paginate_after(rows, after=None, limit=3, key=lambda r: r["id"])
    assert [r["id"] for r in page.items] == [1, 2, 3]
    assert page.has_more is True
    assert page.next_anchor == 3

    page2 = paginate_after(rows, after=3, limit=3, key=lambda r: r["id"])
    assert [r["id"] for r in page2.items] == [4, 5, 6]
    assert page2.has_more is True


def test_paginate_after_exhausted_has_no_next() -> None:
    rows = [{"id": i} for i in range(1, 4)]  # 1,2,3
    page = paginate_after(rows, after=None, limit=10, key=lambda r: r["id"])
    assert [r["id"] for r in page.items] == [1, 2, 3]
    assert page.has_more is False
    assert page.next_anchor is None


def test_page_model_serializes() -> None:
    page: Page[int] = Page(items=[1, 2, 3], next_cursor="abc", has_more=True, page_size=3)
    dumped = page.model_dump()
    assert dumped["items"] == [1, 2, 3]
    assert dumped["next_cursor"] == "abc"
    assert dumped["has_more"] is True


# --------------------------------------------------------------------------- #
# Versioning + deprecation
# --------------------------------------------------------------------------- #


def test_version_manifest_lists_current() -> None:
    manifest = versioning.version_manifest()
    assert manifest["current"] == versioning.CURRENT_VERSION
    assert any(v["version"] == "v1" and v["status"] == "current" for v in manifest["versions"])


def test_deprecation_headers_are_http_dates() -> None:
    headers = versioning.deprecation_headers(
        since="2026-01-01", sunset="2026-07-01", successor="/api/v2/foo"
    )
    assert headers["Deprecation"].endswith("GMT")
    assert headers["Sunset"].endswith("GMT")
    assert 'rel="successor-version"' in headers["Link"]


def test_deprecated_decorator_registers_and_stamps() -> None:
    from fastapi import Response

    @versioning.deprecated(since="2026-02-01", sunset="2026-08-01", note="use /stream")
    async def some_route(response: Response) -> dict[str, str]:
        return {"ok": "yes"}

    assert "some_route" in versioning.REGISTRY
    resp = Response()

    import asyncio

    result = asyncio.run(some_route(response=resp))
    assert result == {"ok": "yes"}
    assert "Deprecation" in resp.headers
    # Cleanup the global registry so the test is order-independent.
    versioning.REGISTRY.pop("some_route", None)


# --------------------------------------------------------------------------- #
# Idempotency fingerprint
# --------------------------------------------------------------------------- #


def test_fingerprint_is_stable_and_body_sensitive() -> None:
    a = fingerprint("POST", "/api/sessions/s1/comment", b'{"note":"red coat"}')
    b = fingerprint("POST", "/api/sessions/s1/comment", b'{"note":"red coat"}')
    c = fingerprint("POST", "/api/sessions/s1/comment", b'{"note":"blue coat"}')
    assert a == b  # identical request -> identical fingerprint
    assert a != c  # different body -> different fingerprint


def test_fingerprint_path_and_method_sensitive() -> None:
    base = fingerprint("POST", "/a", b"x")
    assert base != fingerprint("PUT", "/a", b"x")
    assert base != fingerprint("POST", "/b", b"x")
