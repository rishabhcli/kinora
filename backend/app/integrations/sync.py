"""The sync engine — pull a source incrementally, dedup, and import each item.

The engine is the orchestration heart of the framework. Given a connector, its
context, a starting :class:`~app.integrations.models.SyncCursor`, and an
:class:`ItemImporter` callback (the seam that actually turns a normalized item
into a Kinora book through the §9.1 ingest API), it:

1. **Streams** items from the connector (the connector handles pagination /
   incremental filtering against the cursor).
2. **Dedups** each item via the :class:`DedupStore` seam: an item whose
   ``content_hash`` already exists is skipped; a changed one is re-imported.
3. **Imports** new/changed items through the importer, **isolating per-item
   failures** — one bad article never aborts the run; it is counted as failed
   and the rest proceed.
4. **Retries** transient connector/import failures with the injected
   :class:`~app.integrations.backoff.BackoffPolicy` + clock (honouring any
   ``Retry-After``); permanent failures fail the item immediately.
5. **Advances the cursor** to the newest item seen and returns a
   :class:`SyncReport`.

Everything it touches is a seam (connector, importer, dedup store, clock) so the
whole thing tests with zero network and zero database — see the engine tests.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol, TypeVar, runtime_checkable

from app.core.logging import get_logger
from app.integrations.backoff import BackoffPolicy, is_retryable, retry_after_of
from app.integrations.clock import Clock, SystemClock
from app.integrations.connector import ConnectorContext, SourceConnector
from app.integrations.errors import AuthExpired, TransientError
from app.integrations.models import SourceItem, SyncCursor

logger = get_logger("app.integrations.sync")

_T = TypeVar("_T")


@dataclass(frozen=True)
class DedupDecision:
    """Whether an item should be imported, and why."""

    should_import: bool
    reason: str  # "new" | "changed" | "unchanged"


@runtime_checkable
class DedupStore(Protocol):
    """Decides whether a source item is new/changed/unchanged."""

    async def decide(self, item: SourceItem) -> DedupDecision:
        """Classify ``item`` against what has already been imported."""
        ...

    async def mark_imported(self, item: SourceItem, *, book_id: str | None) -> None:
        """Record that ``item`` was imported (as ``book_id``) for future dedup."""
        ...


class InMemoryDedupStore:
    """A simple content-hash dedup store (used in tests and stateless runs)."""

    def __init__(self, seen: dict[str, str] | None = None) -> None:
        # source_id -> last imported content_hash
        self._seen: dict[str, str] = dict(seen or {})

    async def decide(self, item: SourceItem) -> DedupDecision:
        prior = self._seen.get(item.source_id)
        if prior is None:
            return DedupDecision(should_import=True, reason="new")
        if prior != item.content_hash:
            return DedupDecision(should_import=True, reason="changed")
        return DedupDecision(should_import=False, reason="unchanged")

    async def mark_imported(self, item: SourceItem, *, book_id: str | None) -> None:
        self._seen[item.source_id] = item.content_hash


#: ``import_item(item) -> book_id | None`` — the ingest seam. Returns the created
#: book id (or ``None`` when the importer chose not to create a book).
ItemImporter = Callable[[SourceItem], Awaitable[str | None]]


@dataclass
class ItemOutcome:
    """The per-item result captured for the run report."""

    source_id: str
    title: str
    status: str  # "imported" | "skipped" | "failed"
    reason: str = ""
    book_id: str | None = None


@dataclass
class SyncReport:
    """The summary of one sync run."""

    seen: int = 0
    imported: int = 0
    skipped: int = 0
    failed: int = 0
    cursor: SyncCursor = field(default_factory=SyncCursor)
    outcomes: list[ItemOutcome] = field(default_factory=list)
    #: Set when the *whole run* aborted (e.g. auth expired during fetch).
    fatal_error: str | None = None
    auth_expired: bool = False

    @property
    def status(self) -> str:
        """Coarse run status: success / partial / failed."""
        if self.fatal_error is not None and self.imported == 0:
            return "failed"
        if self.failed > 0 and self.imported > 0:
            return "partial"
        if self.failed > 0 and self.imported == 0:
            return "failed"
        return "success"


class SyncEngine:
    """Drive one incremental, deduped, fault-isolated sync."""

    def __init__(
        self,
        *,
        backoff: BackoffPolicy | None = None,
        clock: Clock | None = None,
        rand: Callable[[], float] | None = None,
        max_items: int = 1000,
    ) -> None:
        self._backoff = backoff or BackoffPolicy()
        self._clock = clock or SystemClock()
        self._rand = rand
        self._max_items = max_items

    async def run(
        self,
        connector: SourceConnector,
        ctx: ConnectorContext,
        cursor: SyncCursor,
        importer: ItemImporter,
        dedup: DedupStore,
    ) -> SyncReport:
        """Execute one sync; never raises for per-item errors (they are counted)."""
        report = SyncReport(cursor=cursor)
        max_seen = cursor.high_watermark
        page_etag = cursor.etag

        # Stream items, with whole-fetch retry/backoff handled around iteration.
        try:
            items, page_etag = await self._collect_items(connector, ctx, cursor)
        except AuthExpired as exc:
            report.fatal_error = str(exc)
            report.auth_expired = True
            logger.warning("integrations.sync.auth_expired", provider=connector.info().name)
            return report
        except TransientError as exc:
            # Exhausted retries fetching — fatal for this run, retry next time.
            report.fatal_error = str(exc)
            logger.warning("integrations.sync.fetch_failed", error=str(exc))
            return report

        for item in items:
            if report.seen >= self._max_items:
                logger.info("integrations.sync.max_items", limit=self._max_items)
                break
            report.seen += 1
            max_seen = _max_dt(max_seen, item.updated_at)
            try:
                outcome = await self._process_item(item, importer, dedup)
            except AuthExpired as exc:
                # An auth failure mid-import is fatal: stop, surface for re-auth.
                report.fatal_error = str(exc)
                report.auth_expired = True
                break
            report.outcomes.append(outcome)
            if outcome.status == "imported":
                report.imported += 1
            elif outcome.status == "skipped":
                report.skipped += 1
            else:
                report.failed += 1

        report.cursor = SyncCursor(high_watermark=max_seen, etag=page_etag, opaque=cursor.opaque)
        return report

    async def _collect_items(
        self, connector: SourceConnector, ctx: ConnectorContext, cursor: SyncCursor
    ) -> tuple[list[SourceItem], str | None]:
        """Collect every item with whole-fetch retry; capture the first page etag.

        The connector's ``iter_items`` walks pagination; we wrap the *whole*
        iteration in the retry loop because connectors fetch lazily and a
        mid-stream transient should restart cleanly from the cursor.
        """

        async def _attempt() -> tuple[list[SourceItem], str | None]:
            collected: list[SourceItem] = []
            etag = cursor.etag
            # Grab the first page directly so we can read its etag, then continue.
            first = await connector.fetch_page(ctx, cursor, None)
            if first.etag is not None and first.etag == cursor.etag:
                return [], cursor.etag  # unchanged feed
            etag = first.etag if first.etag is not None else etag
            collected.extend(first.items)
            page_token = first.next_cursor
            while page_token is not None and len(collected) < self._max_items:
                page = await connector.fetch_page(ctx, cursor, page_token)
                collected.extend(page.items)
                page_token = page.next_cursor
            return collected, etag

        return await self._with_retry(_attempt)

    async def _process_item(
        self, item: SourceItem, importer: ItemImporter, dedup: DedupStore
    ) -> ItemOutcome:
        """Dedup + import one item, isolating its failure from the rest."""
        title = item.document.title
        decision = await dedup.decide(item)
        if not decision.should_import:
            return ItemOutcome(item.source_id, title, "skipped", reason=decision.reason)
        try:
            book_id = await self._with_retry(lambda: importer(item))
        except AuthExpired:
            raise  # propagate — the run handler treats it as fatal
        except Exception as exc:  # noqa: BLE001 - isolate a per-item failure
            logger.warning(
                "integrations.sync.item_failed", source_id=item.source_id, error=str(exc)
            )
            return ItemOutcome(item.source_id, title, "failed", reason=str(exc)[:200])
        await dedup.mark_imported(item, book_id=book_id)
        return ItemOutcome(
            item.source_id, title, "imported", reason=decision.reason, book_id=book_id
        )

    async def _with_retry(self, fn: Callable[[], Awaitable[_T]]) -> _T:
        """Run ``fn`` with the backoff policy; re-raise the last error on exhaust."""
        attempt = 0
        last: BaseException | None = None
        while attempt <= self._backoff.max_attempts:
            try:
                return await fn()
            except Exception as exc:  # noqa: BLE001 - classify then maybe retry
                last = exc
                if not is_retryable(exc) or attempt >= self._backoff.max_attempts:
                    raise
                delay = self._backoff.delay(
                    attempt, retry_after_s=retry_after_of(exc), rand=self._rand
                )
                logger.info("integrations.sync.retry", attempt=attempt + 1, delay_s=round(delay, 2))
                await self._clock.sleep(delay)
                attempt += 1
        assert last is not None  # pragma: no cover - loop always raises or returns
        raise last


def _max_dt(a: datetime | None, b: datetime | None) -> datetime | None:
    """Return the later of two optional datetimes."""
    if a is None:
        return b
    if b is None:
        return a
    return a if a >= b else b


__all__ = [
    "DedupDecision",
    "DedupStore",
    "InMemoryDedupStore",
    "ItemImporter",
    "ItemOutcome",
    "SyncEngine",
    "SyncReport",
]
