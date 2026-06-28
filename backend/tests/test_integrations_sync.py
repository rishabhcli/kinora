"""Unit tests for the sync engine (offline; fake connector + clock).

Covers: happy-path import, content-hash dedup (new/changed/unchanged),
incremental cursor advance, per-item failure isolation, transient-fetch retry
with backoff, rate-limit retry-after, permanent-error item failure, and the
auth-expiry fatal path.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.integrations.backoff import BackoffPolicy
from app.integrations.clock import FakeClock
from app.integrations.connector import (
    Capability,
    ConnectorContext,
    ConnectorInfo,
    SourceConnector,
)
from app.integrations.errors import AuthExpired, PermanentError, RateLimited, TransientError
from app.integrations.models import (
    BlockKind,
    FetchPage,
    NormalizedBlock,
    NormalizedDocument,
    SourceItem,
    SyncCursor,
)
from app.integrations.sync import InMemoryDedupStore, SyncEngine


def _item(source_id: str, text: str, when: datetime | None = None) -> SourceItem:
    doc = NormalizedDocument(
        title=source_id, blocks=(NormalizedBlock(kind=BlockKind.PARAGRAPH, text=text),)
    )
    return SourceItem(source_id=source_id, document=doc, updated_at=when)


class _StubConnector(SourceConnector):
    """A connector that replays scripted pages / errors per fetch call."""

    def __init__(self, pages: list[object]) -> None:
        self._pages = list(pages)
        self.calls = 0

    @classmethod
    def info(cls) -> ConnectorInfo:
        return ConnectorInfo(
            name="stub", display_name="Stub", capabilities=frozenset({Capability.INCREMENTAL})
        )

    async def fetch_page(self, ctx, cursor, page_token):  # type: ignore[no-untyped-def]
        self.calls += 1
        nxt = self._pages.pop(0) if self._pages else FetchPage()
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


def _ctx() -> ConnectorContext:
    from app.integrations.http import FakeHttpClient

    return ConnectorContext(http=FakeHttpClient())


def _engine() -> tuple[SyncEngine, list[str]]:
    imported: list[str] = []

    return (
        SyncEngine(
            backoff=BackoffPolicy(base_s=0.1, max_attempts=3, jitter=0.0),
            clock=FakeClock(),
            rand=lambda: 0.5,
        ),
        imported,
    )


def _importer(imported: list[str], fail: set[str] | None = None, raise_for=None):  # type: ignore[no-untyped-def]
    fail = fail or set()

    async def importer(item: SourceItem) -> str | None:
        if raise_for is not None and item.source_id in raise_for:
            raise raise_for[item.source_id]
        if item.source_id in fail:
            raise RuntimeError("import boom")
        imported.append(item.source_id)
        return f"book_{item.source_id}"

    return importer


@pytest.mark.asyncio
async def test_happy_path_imports_all() -> None:
    engine, imported = _engine()
    connector = _StubConnector([FetchPage(items=(_item("a", "x"), _item("b", "y")))])
    report = await engine.run(
        connector, _ctx(), SyncCursor(), _importer(imported), InMemoryDedupStore()
    )
    assert report.imported == 2 and report.failed == 0 and report.skipped == 0
    assert report.status == "success"
    assert imported == ["a", "b"]


@pytest.mark.asyncio
async def test_dedup_skips_unchanged_reimports_changed() -> None:
    engine, imported = _engine()
    store = InMemoryDedupStore()
    # First sync imports a.
    await engine.run(
        _StubConnector([FetchPage(items=(_item("a", "v1"),))]),
        _ctx(), SyncCursor(), _importer(imported), store,
    )
    assert imported == ["a"]
    # Unchanged content -> skipped.
    r2 = await engine.run(
        _StubConnector([FetchPage(items=(_item("a", "v1"),))]),
        _ctx(), SyncCursor(), _importer(imported), store,
    )
    assert r2.skipped == 1 and r2.imported == 0
    # Changed content -> re-imported.
    r3 = await engine.run(
        _StubConnector([FetchPage(items=(_item("a", "v2-changed"),))]),
        _ctx(), SyncCursor(), _importer(imported), store,
    )
    assert r3.imported == 1
    assert imported == ["a", "a"]


@pytest.mark.asyncio
async def test_cursor_advances_to_newest_seen() -> None:
    engine, imported = _engine()
    t1 = datetime(2026, 1, 1, tzinfo=UTC)
    t2 = datetime(2026, 1, 5, tzinfo=UTC)
    connector = _StubConnector([FetchPage(items=(_item("a", "x", t1), _item("b", "y", t2)))])
    report = await engine.run(
        connector, _ctx(), SyncCursor(), _importer(imported), InMemoryDedupStore()
    )
    assert report.cursor.high_watermark == t2


@pytest.mark.asyncio
async def test_per_item_failure_is_isolated() -> None:
    engine, imported = _engine()
    connector = _StubConnector(
        [FetchPage(items=(_item("ok1", "x"), _item("bad", "y"), _item("ok2", "z")))]
    )
    report = await engine.run(
        connector, _ctx(), SyncCursor(), _importer(imported, fail={"bad"}), InMemoryDedupStore()
    )
    assert report.imported == 2 and report.failed == 1
    assert report.status == "partial"
    assert imported == ["ok1", "ok2"]  # the good ones still imported


@pytest.mark.asyncio
async def test_pagination_walks_next_cursor() -> None:
    engine, imported = _engine()
    connector = _StubConnector(
        [
            FetchPage(items=(_item("a", "x"),), next_cursor="p2"),
            FetchPage(items=(_item("b", "y"),), next_cursor=None),
        ]
    )
    report = await engine.run(
        connector, _ctx(), SyncCursor(), _importer(imported), InMemoryDedupStore()
    )
    assert report.imported == 2
    assert imported == ["a", "b"]


@pytest.mark.asyncio
async def test_transient_fetch_retries_then_succeeds() -> None:
    engine, imported = _engine()
    connector = _StubConnector(
        [TransientError("blip"), TransientError("blip"), FetchPage(items=(_item("a", "x"),))]
    )
    report = await engine.run(
        connector, _ctx(), SyncCursor(), _importer(imported), InMemoryDedupStore()
    )
    assert report.imported == 1
    # Two retries -> two recorded sleeps on the fake clock.
    assert isinstance(engine._clock, FakeClock)
    assert len(engine._clock.slept) == 2


@pytest.mark.asyncio
async def test_rate_limited_uses_retry_after() -> None:
    engine, imported = _engine()
    connector = _StubConnector(
        [RateLimited("slow down", retry_after_s=7.0), FetchPage(items=(_item("a", "x"),))]
    )
    await engine.run(connector, _ctx(), SyncCursor(), _importer(imported), InMemoryDedupStore())
    assert isinstance(engine._clock, FakeClock)
    assert engine._clock.slept == [7.0]


@pytest.mark.asyncio
async def test_transient_fetch_exhausts_and_fails_run() -> None:
    engine, imported = _engine()
    connector = _StubConnector([TransientError("down")] * 10)
    report = await engine.run(
        connector, _ctx(), SyncCursor(), _importer(imported), InMemoryDedupStore()
    )
    assert report.fatal_error is not None
    assert report.status == "failed"
    assert report.imported == 0


@pytest.mark.asyncio
async def test_permanent_item_error_fails_just_that_item() -> None:
    engine, imported = _engine()
    connector = _StubConnector([FetchPage(items=(_item("a", "x"), _item("b", "y")))])
    report = await engine.run(
        connector, _ctx(), SyncCursor(),
        _importer(imported, raise_for={"a": PermanentError("nope")}),
        InMemoryDedupStore(),
    )
    assert report.failed == 1 and report.imported == 1
    # No retry of a permanent failure.
    assert isinstance(engine._clock, FakeClock)
    assert engine._clock.slept == []


@pytest.mark.asyncio
async def test_auth_expiry_during_import_is_fatal() -> None:
    engine, imported = _engine()
    connector = _StubConnector([FetchPage(items=(_item("a", "x"), _item("b", "y")))])
    report = await engine.run(
        connector, _ctx(), SyncCursor(),
        _importer(imported, raise_for={"a": AuthExpired("expired")}),
        InMemoryDedupStore(),
    )
    assert report.auth_expired and report.fatal_error is not None
    # Stopped at the first item — b never processed.
    assert "b" not in imported
