"""Async Postgres-backed persistence for flags, experiments, exposures, audit.

Repositories follow the project convention (:class:`~app.db.repositories.base.BaseRepository`):
they hold an :class:`AsyncSession`, *flush* (never commit) so the unit-of-work
boundary owns the transaction. They translate between the serialized JSONB rows
and the pure model types via :mod:`app.flags.serialization`, so callers always
work with validated :class:`~app.flags.models.Flag` / :class:`~app.flags.experiment.Experiment`
objects, never raw dicts.

Writes are *versioned and audited*: every flag/experiment save bumps the version
and appends a :class:`~app.flags.db_models.FlagAudit` row with a computed diff
(:mod:`app.flags.audit`). Exposure logging is idempotent via the ``dedup_key``
UNIQUE constraint — a duplicate insert is swallowed (ON CONFLICT DO NOTHING) so
the same unit is counted at most once per experiment version.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, cast

from sqlalchemy import CursorResult, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.db.base import new_id
from app.db.repositories.base import BaseRepository
from app.flags.audit import AuditAction, diff, infer_action, summarize
from app.flags.db_models import FeatureFlag, FlagAudit, FlagExperiment, FlagExposure
from app.flags.errors import FlagNotFoundError
from app.flags.experiment import Experiment
from app.flags.models import Flag, FlagSnapshot
from app.flags.serialization import (
    experiment_from_dict,
    experiment_to_dict,
    flag_from_dict,
    flag_to_dict,
)


class FlagStore(BaseRepository):
    """CRUD for durable flags + the audit trail."""

    async def get(self, key: str) -> Flag | None:
        """Load and deserialize the flag with ``key`` (``None`` if absent)."""
        row = await self._row(key)
        return flag_from_dict(row.definition) if row is not None else None

    async def require(self, key: str) -> Flag:
        """Like :meth:`get` but raises :class:`FlagNotFoundError` when absent."""
        flag = await self.get(key)
        if flag is None:
            raise FlagNotFoundError(f"flag {key!r} not found")
        return flag

    async def list_all(self, *, include_archived: bool = False) -> list[Flag]:
        """All flags (optionally including archived), sorted by key."""
        stmt = select(FeatureFlag)
        if not include_archived:
            stmt = stmt.where(FeatureFlag.archived.is_(False))
        stmt = stmt.order_by(FeatureFlag.key)
        rows = (await self.session.execute(stmt)).scalars().all()
        return [flag_from_dict(r.definition) for r in rows]

    async def load_snapshot(self, *, version: int = 0) -> FlagSnapshot:
        """Build an in-memory :class:`FlagSnapshot` from every stored flag.

        Archived flags are *included* so prerequisite resolution against an
        archived dependency still works deterministically (the evaluator handles
        archived state itself). ``version`` stamps the snapshot for cache use.
        """
        rows = (await self.session.execute(select(FeatureFlag))).scalars().all()
        flags = tuple(flag_from_dict(r.definition) for r in rows)
        return FlagSnapshot.from_flags(flags, version=version)

    async def save(self, flag: Flag, *, actor: str | None = None) -> Flag:
        """Upsert ``flag``, bumping its version and appending an audit record.

        Returns the saved flag stamped with its new version. The stored
        ``definition`` always carries the *new* version so a reload is faithful.
        """
        row = await self._row(flag.key)
        before = row.definition if row is not None else None
        new_version = (row.version + 1) if row is not None else 1
        saved = flag.with_version(new_version)
        definition = flag_to_dict(saved)

        if row is None:
            row = FeatureFlag(id=new_id(), key=flag.key)
            self.session.add(row)
        row.kind = saved.kind.value
        row.enabled = saved.enabled
        row.archived = saved.archived
        row.version = new_version
        row.definition = definition
        row.name = saved.name
        row.description = saved.description

        await self._audit("flag", flag.key, before, definition, actor=actor)
        await self.session.flush()
        return saved

    async def set_enabled(self, key: str, enabled: bool, *, actor: str | None = None) -> Flag:
        """Toggle a flag's kill switch (a cheap, common, audited mutation)."""
        flag = await self.require(key)
        return await self.save(replace(flag, enabled=enabled), actor=actor)

    async def archive(self, key: str, *, actor: str | None = None) -> Flag:
        """Archive a flag (soft-delete; keeps history and prerequisite resolution)."""
        flag = await self.require(key)
        return await self.save(replace(flag, archived=True), actor=actor)

    async def delete(self, key: str, *, actor: str | None = None) -> bool:
        """Hard-delete a flag and append a DELETE audit; returns whether it existed."""
        row = await self._row(key)
        if row is None:
            return False
        await self._audit("flag", key, row.definition, None, actor=actor)
        await self.session.delete(row)
        await self.session.flush()
        return True

    async def _row(self, key: str) -> FeatureFlag | None:
        stmt = select(FeatureFlag).where(FeatureFlag.key == key)
        return (await self.session.execute(stmt)).scalars().first()

    async def _audit(
        self,
        subject_kind: str,
        subject_key: str,
        before: dict[str, Any] | None,
        after: dict[str, Any] | None,
        *,
        actor: str | None,
    ) -> None:
        changes = diff(before, after)
        action = infer_action(before, after)
        self.session.add(
            FlagAudit(
                id=new_id(),
                subject_kind=subject_kind,
                subject_key=subject_key,
                action=action.value,
                actor=actor,
                summary=summarize(changes),
                before=before,
                after=after,
                changes=[c.to_dict() for c in changes],
            )
        )

    async def audit_log(
        self, *, subject_key: str | None = None, limit: int = 50
    ) -> list[FlagAudit]:
        """Recent audit records (newest first), optionally for one subject."""
        stmt = select(FlagAudit)
        if subject_key is not None:
            stmt = stmt.where(FlagAudit.subject_key == subject_key)
        stmt = stmt.order_by(FlagAudit.created_at.desc()).limit(limit)
        return list((await self.session.execute(stmt)).scalars().all())


class ExperimentStore(BaseRepository):
    """CRUD for durable experiments + idempotent exposure logging."""

    async def get(self, key: str) -> Experiment | None:
        row = await self._row(key)
        return experiment_from_dict(row.definition) if row is not None else None

    async def list_all(self) -> list[Experiment]:
        stmt = select(FlagExperiment).order_by(FlagExperiment.key)
        rows = (await self.session.execute(stmt)).scalars().all()
        return [experiment_from_dict(r.definition) for r in rows]

    async def save(self, experiment: Experiment, *, actor: str | None = None) -> Experiment:
        """Upsert an experiment, bumping version + appending audit."""
        row = await self._row(experiment.key)
        before = row.definition if row is not None else None
        new_version = (row.version + 1) if row is not None else 1
        saved = replace(experiment, version=new_version)
        definition = experiment_to_dict(saved)

        if row is None:
            row = FlagExperiment(id=new_id(), key=experiment.key)
            self.session.add(row)
        row.status = saved.status.value
        row.version = new_version
        row.definition = definition
        row.name = saved.name
        row.description = saved.description

        changes = diff(before, definition)
        self.session.add(
            FlagAudit(
                id=new_id(),
                subject_kind="experiment",
                subject_key=experiment.key,
                action=(AuditAction.CREATE if before is None else AuditAction.UPDATE).value,
                actor=actor,
                summary=summarize(changes),
                before=before,
                after=definition,
                changes=[c.to_dict() for c in changes],
            )
        )
        await self.session.flush()
        return saved

    async def log_exposure(
        self,
        *,
        experiment_key: str,
        experiment_version: int,
        variant_key: str,
        unit_key: str,
        dedup_key: str,
        context: dict[str, Any] | None = None,
    ) -> bool:
        """Record an exposure idempotently; returns whether a new row was inserted.

        Uses ``INSERT ... ON CONFLICT (dedup_key) DO NOTHING`` so concurrent
        loggers and retries never double-count a unit.
        """
        stmt = (
            pg_insert(FlagExposure)
            .values(
                id=new_id(),
                experiment_key=experiment_key,
                experiment_version=experiment_version,
                variant_key=variant_key,
                unit_key=unit_key,
                dedup_key=dedup_key,
                context=context or {},
            )
            .on_conflict_do_nothing(constraint="uq_flag_exposures_dedup_key")
        )
        result = cast("CursorResult[Any]", await self.session.execute(stmt))
        await self.session.flush()
        return bool(result.rowcount or 0)

    async def exposure_counts(self, experiment_key: str) -> dict[str, int]:
        """Distinct-unit exposure count per variant for an experiment."""
        stmt = select(FlagExposure.variant_key).where(
            FlagExposure.experiment_key == experiment_key
        )
        rows = (await self.session.execute(stmt)).scalars().all()
        counts: dict[str, int] = {}
        for variant in rows:
            counts[variant] = counts.get(variant, 0) + 1
        return counts

    async def _row(self, key: str) -> FlagExperiment | None:
        stmt = select(FlagExperiment).where(FlagExperiment.key == key)
        return (await self.session.execute(stmt)).scalars().first()


__all__ = ["ExperimentStore", "FlagStore"]
