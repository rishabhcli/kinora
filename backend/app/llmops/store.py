"""DB-backed persistence for the LLM-ops platform.

The pure in-memory components (:mod:`registry`, :mod:`tracing`) are the source of
truth at runtime; this module is the durable mirror over the four
``llmops_*`` tables (:mod:`app.db.models.llmops`). It follows the repo's
repository convention: it *flushes* but never *commits* — the unit-of-work
boundary owns the transaction (see ``app.db.repositories.base``).

Three repositories:

* :class:`PromptVersionStore` — persist a :class:`~app.llmops.registry.PromptRecord`
  + changelog entries; load a registry back out of the DB (the
  ``hydrate_registry`` round-trip that survives a restart).
* :class:`RunTraceStore` — persist a :class:`~app.llmops.tracing.RunTrace` and run
  the query API against the DB with the same :class:`~app.llmops.tracing.TraceQuery`
  filter, plus a SQL-side aggregate.
* :class:`EvalReportStore` — persist + fetch cached eval / A-B / regression report
  JSON bodies.

Every method is async and infra-bound; the unit suite skips these cleanly when no
test DB is configured (the pure logic is fully covered without them).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import new_id
from app.db.models.llmops import (
    LLMOpsChangelog,
    LLMOpsEvalReport,
    LLMOpsPromptVersion,
    LLMOpsRun,
)
from app.llmops.registry import (
    ChangeKind,
    ChangelogEntry,
    PromptRecord,
    PromptRegistry,
    VersionStatus,
)
from app.llmops.tracing import RunTrace, TraceAggregate, TraceQuery, aggregate


class PromptVersionStore:
    """Persist + load the prompt registry over the DB."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def upsert_record(self, record: PromptRecord) -> None:
        """Insert a prompt version if its ``(key, version)`` is not already stored."""
        existing = await self.session.scalar(
            select(LLMOpsPromptVersion).where(
                LLMOpsPromptVersion.prompt_key == record.key,
                LLMOpsPromptVersion.version == record.version,
            )
        )
        if existing is not None:
            existing.status = record.status.value
            existing.system = record.system
            existing.sha256 = record.sha256
            existing.prompt_tag = record.prompt_tag
        else:
            self.session.add(
                LLMOpsPromptVersion(
                    id=new_id(),
                    prompt_key=record.key,
                    version=record.version,
                    prompt_tag=record.prompt_tag,
                    system=record.system,
                    sha256=record.sha256,
                    status=record.status.value,
                    created_at=record.created_at,
                )
            )
        await self.session.flush()

    async def add_changelog(self, entry: ChangelogEntry) -> None:
        self.session.add(
            LLMOpsChangelog(
                id=new_id(),
                prompt_key=entry.key,
                version=entry.version,
                kind=entry.kind.value,
                summary=entry.summary,
                author=entry.author,
                created_at=entry.created_at,
            )
        )
        await self.session.flush()

    async def persist_registry(self, registry: PromptRegistry) -> None:
        """Write every record + changelog entry of a registry to the DB."""
        for record in registry.export_records():
            await self.upsert_record(record)
        # Mark the active version per key.
        for key in registry.keys():  # noqa: SIM118 - PromptRegistry.keys() is a method, not a dict
            active = registry.get_active(key)
            await self._set_active(key, active.version)
        for entry in registry.changelog():
            await self.add_changelog(entry)

    async def _set_active(self, key: str, version: str) -> None:
        rows = (
            await self.session.scalars(
                select(LLMOpsPromptVersion).where(LLMOpsPromptVersion.prompt_key == key)
            )
        ).all()
        for row in rows:
            if row.version == version:
                row.status = VersionStatus.ACTIVE.value
            elif row.status == VersionStatus.ACTIVE.value:
                row.status = VersionStatus.ARCHIVED.value
        await self.session.flush()

    async def hydrate_registry(self) -> PromptRegistry:
        """Rebuild a :class:`PromptRegistry` from the persisted rows + changelog."""
        registry = PromptRegistry()
        rows = (
            await self.session.scalars(
                select(LLMOpsPromptVersion).order_by(
                    LLMOpsPromptVersion.prompt_key, LLMOpsPromptVersion.created_at
                )
            )
        ).all()
        for row in rows:
            record = PromptRecord(
                key=row.prompt_key,
                version=row.version,
                prompt_tag=row.prompt_tag,
                system=row.system,
                sha256=row.sha256,
                status=VersionStatus(row.status),
                created_at=row.created_at,
            )
            registry._records.setdefault(record.key, {})[record.version] = record
            if record.status is VersionStatus.ACTIVE:
                registry._active[record.key] = record.version
        log_rows = (
            await self.session.scalars(select(LLMOpsChangelog).order_by(LLMOpsChangelog.created_at))
        ).all()
        registry._changelog = [
            ChangelogEntry(
                key=r.prompt_key,
                version=r.version,
                kind=ChangeKind(r.kind),
                summary=r.summary,
                author=r.author,
                created_at=r.created_at,
            )
            for r in log_rows
        ]
        return registry


class RunTraceStore:
    """Persist run traces + the query API over the DB."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def record(self, trace: RunTrace) -> None:
        self.session.add(
            LLMOpsRun(
                id=trace.id,
                prompt_key=trace.prompt_key,
                prompt_version=trace.prompt_version,
                model=trace.model,
                input_tokens=trace.input_tokens,
                output_tokens=trace.output_tokens,
                cost_usd=trace.cost_usd,
                latency_ms=trace.latency_ms,
                inputs=trace.inputs or None,
                output=trace.output or None,
                guardrail_decision=trace.guardrail_decision,
                cache_hit=trace.cache_hit,
                error=trace.error,
                book_id=trace.book_id,
                session_id=trace.session_id,
                created_at=trace.created_at,
            )
        )
        await self.session.flush()

    @staticmethod
    def _row_to_trace(row: LLMOpsRun) -> RunTrace:
        return RunTrace(
            id=row.id,
            prompt_key=row.prompt_key,
            prompt_version=row.prompt_version,
            model=row.model,
            input_tokens=row.input_tokens,
            output_tokens=row.output_tokens,
            cost_usd=Decimal(str(row.cost_usd)),
            latency_ms=row.latency_ms,
            created_at=row.created_at,
            inputs=dict(row.inputs) if row.inputs else {},
            output=row.output or "",
            guardrail_decision=row.guardrail_decision,
            book_id=row.book_id,
            session_id=row.session_id,
            cache_hit=row.cache_hit,
            error=row.error,
        )

    async def query(self, q: TraceQuery) -> list[RunTrace]:
        """Run a :class:`TraceQuery` against the DB (SQL-side filters + limit)."""
        stmt = select(LLMOpsRun)
        if q.prompt_key is not None:
            stmt = stmt.where(LLMOpsRun.prompt_key == q.prompt_key)
        if q.prompt_version is not None:
            stmt = stmt.where(LLMOpsRun.prompt_version == q.prompt_version)
        if q.model is not None:
            stmt = stmt.where(LLMOpsRun.model == q.model)
        if q.book_id is not None:
            stmt = stmt.where(LLMOpsRun.book_id == q.book_id)
        if q.session_id is not None:
            stmt = stmt.where(LLMOpsRun.session_id == q.session_id)
        if q.since is not None:
            stmt = stmt.where(LLMOpsRun.created_at >= q.since)
        if q.until is not None:
            stmt = stmt.where(LLMOpsRun.created_at <= q.until)
        if q.min_cost_usd is not None:
            stmt = stmt.where(LLMOpsRun.cost_usd >= q.min_cost_usd)
        if q.errors_only:
            stmt = stmt.where(LLMOpsRun.error.is_not(None))
        if q.cache_hits_only is not None:
            stmt = stmt.where(LLMOpsRun.cache_hit.is_(q.cache_hits_only))
        stmt = stmt.order_by(
            LLMOpsRun.created_at.desc() if q.newest_first else LLMOpsRun.created_at.asc()
        )
        if q.limit is not None:
            stmt = stmt.limit(q.limit)
        rows = (await self.session.scalars(stmt)).all()
        return [self._row_to_trace(r) for r in rows]

    async def get(self, trace_id: str) -> RunTrace | None:
        row = await self.session.get(LLMOpsRun, trace_id)
        return self._row_to_trace(row) if row is not None else None

    async def aggregate(self, q: TraceQuery) -> TraceAggregate:
        """Roll up the traces matching ``q`` (loads then aggregates in Python).

        Latency percentiles are easier (and portable) computed in Python over the
        matched rows; the filter still runs SQL-side via :meth:`query`.
        """
        # Aggregation ignores the query's limit so totals are complete.
        traces = await self.query(q.all_matching())
        return aggregate(traces)


class EvalReportStore:
    """Persist + fetch cached eval / A-B / regression report bodies."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def save(
        self, *, kind: str, prompt_key: str, dataset_name: str, body: dict[str, Any]
    ) -> str:
        report_id = f"rep_{new_id()[:16]}"
        self.session.add(
            LLMOpsEvalReport(
                id=report_id,
                kind=kind,
                prompt_key=prompt_key,
                dataset_name=dataset_name,
                body=body,
            )
        )
        await self.session.flush()
        return report_id

    async def get(self, report_id: str) -> dict[str, Any] | None:
        row = await self.session.get(LLMOpsEvalReport, report_id)
        return dict(row.body) if row is not None else None

    async def latest_for(
        self, prompt_key: str, *, kind: str | None = None
    ) -> dict[str, Any] | None:
        stmt = select(LLMOpsEvalReport).where(LLMOpsEvalReport.prompt_key == prompt_key)
        if kind is not None:
            stmt = stmt.where(LLMOpsEvalReport.kind == kind)
        stmt = stmt.order_by(LLMOpsEvalReport.created_at.desc()).limit(1)
        row = await self.session.scalar(stmt)
        return dict(row.body) if row is not None else None


__all__ = ["EvalReportStore", "PromptVersionStore", "RunTraceStore"]
