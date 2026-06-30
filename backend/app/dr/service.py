"""The orchestrating disaster-recovery facade.

:class:`DRService` is the single entry point an operator (or a scheduler) drives:
it owns the injected seams (event source/sink, canon, read models, asset source,
the backup repository) and the :class:`~app.dr.config.DRConfig`, and exposes the
high-level verbs that compose the lower modules:

* :meth:`backup_full` / :meth:`backup_incremental` — capture a tier and persist
  it to the repository (the incremental auto-resolves its parent = the freshest
  backup, or an explicit one).
* :meth:`restore` — restore the latest (or a named) chain, with dry-run.
* :meth:`recover_to_position` / :meth:`recover_to_timestamp` — point-in-time.
* :meth:`gc` — apply retention.
* :meth:`health` — the fleet health report.
* :meth:`rpo_rto` — accounting for the freshest recoverable point right now.

The service holds no mutable state of its own beyond the injected collaborators,
so a test constructs it with the in-memory fakes and exercises the whole flow
deterministically. An id generator + a clock are injected (defaulting to a
monotonic counter and ``datetime.now(UTC)``) so backups get stable ids and the
accounting is reproducible under test.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime

import structlog

from app.dr import accounting, pitr, retention, tiers
from app.dr import restore as restore_mod
from app.dr.config import DRConfig
from app.dr.errors import ChainError
from app.dr.interfaces import (
    AssetSource,
    BackupRepository,
    CanonSource,
    EventSink,
    EventSource,
    ReadModelTarget,
)
from app.dr.models import BackupHealth, BackupManifest, RPORTOReport
from app.dr.restore import Projector, RestorePlan, RestoreResult

logger = structlog.get_logger(__name__)


class DRService:
    """Coordinates backup, restore, PITR, retention, and reporting."""

    def __init__(
        self,
        *,
        repo: BackupRepository,
        event_source: EventSource,
        canon: CanonSource,
        read_models: ReadModelTarget,
        assets: AssetSource,
        event_sink: EventSink | None = None,
        config: DRConfig | None = None,
        clock: Callable[[], datetime] | None = None,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        self._repo = repo
        self._event_source = event_source
        self._canon = canon
        self._read_models = read_models
        self._assets = assets
        self._event_sink = event_sink
        self._config = config or DRConfig()
        self._clock = clock or (lambda: datetime.now(UTC))
        self._seq = 0
        self._id_factory = id_factory or self._default_id

    def _default_id(self) -> str:
        self._seq += 1
        return f"bk_{self._seq:08d}"

    # -- capture ------------------------------------------------------------- #

    async def backup_full(
        self,
        *,
        checkpoints: Mapping[str, int] | None = None,
        labels: Mapping[str, str] | None = None,
    ) -> BackupManifest:
        """Capture + persist a full backup."""
        manifest = await tiers.make_full(
            snapshot_id=self._id_factory(),
            event_source=self._event_source,
            canon=self._canon,
            read_models=self._read_models,
            assets=self._assets,
            checkpoints=checkpoints,
            now=self._clock(),
            labels=labels,
        )
        await self._repo.save(manifest)
        return manifest

    async def backup_incremental(
        self,
        *,
        parent_id: str | None = None,
        checkpoints: Mapping[str, int] | None = None,
        labels: Mapping[str, str] | None = None,
    ) -> BackupManifest:
        """Capture + persist an incremental against ``parent_id`` (or the freshest).

        Raises:
            ChainError: there is no backup to chain onto.
        """
        parent = await self._resolve_parent(parent_id)
        manifest = await tiers.make_incremental(
            snapshot_id=self._id_factory(),
            parent=parent,
            event_source=self._event_source,
            canon=self._canon,
            read_models=self._read_models,
            assets=self._assets,
            checkpoints=checkpoints,
            now=self._clock(),
            labels=labels,
        )
        await self._repo.save(manifest)
        return manifest

    async def _resolve_parent(self, parent_id: str | None) -> BackupManifest:
        if parent_id is not None:
            parent = await self._repo.get(parent_id)
            if parent is None:
                raise ChainError(f"parent backup {parent_id!r} does not exist")
            return parent
        latest = await self._latest_backup()
        if latest is None:
            raise ChainError("no backup exists to chain an incremental onto")
        return latest

    async def _latest_backup(self) -> BackupManifest | None:
        manifests = await self._all()
        if not manifests:
            return None
        return max(manifests, key=lambda m: m.descriptor.created_at)

    async def _all(self) -> list[BackupManifest]:
        out: list[BackupManifest] = []
        for sid in await self._repo.list_ids():
            m = await self._repo.get(sid)
            if m is not None:
                out.append(m)
        return out

    # -- restore ------------------------------------------------------------- #

    async def restore(
        self,
        head_id: str | None = None,
        *,
        projector: Projector | None = None,
        dry_run: bool = False,
        require_assets: bool = True,
        through: int | None = None,
    ) -> tuple[RestorePlan, RestoreResult | None]:
        """Restore the latest (or a named) chain. Requires an event sink."""
        if self._event_sink is None:
            raise ChainError("restore requires an event_sink to be injected")
        target_id = head_id or await self._latest_id()
        return await restore_mod.restore(
            self._repo,
            target_id,
            event_sink=self._event_sink,
            canon=self._canon,
            read_models=self._read_models,
            assets=self._assets,
            through=through,
            projector=projector,
            dry_run=dry_run,
            require_assets=require_assets,
        )

    async def _latest_id(self) -> str:
        latest = await self._latest_backup()
        if latest is None:
            raise ChainError("no backup exists to restore from")
        return latest.descriptor.snapshot_id

    async def recover_to_position(
        self,
        position: int,
        *,
        projector: Projector,
        dry_run: bool = False,
        require_assets: bool = True,
    ) -> tuple[pitr.RecoveryTarget, RestorePlan, RestoreResult | None]:
        """Point-in-time recovery to event ``position``."""
        if self._event_sink is None:
            raise ChainError("recovery requires an event_sink to be injected")
        return await pitr.recover_to_position(
            self._repo,
            position,
            event_sink=self._event_sink,
            canon=self._canon,
            read_models=self._read_models,
            assets=self._assets,
            projector=projector,
            dry_run=dry_run,
            require_assets=require_assets,
        )

    async def recover_to_timestamp(
        self,
        timestamp: float,
        *,
        projector: Projector,
        dry_run: bool = False,
        require_assets: bool = True,
    ) -> tuple[pitr.RecoveryTarget, RestorePlan, RestoreResult | None]:
        """Point-in-time recovery to wall-clock ``timestamp`` (epoch s)."""
        if self._event_sink is None:
            raise ChainError("recovery requires an event_sink to be injected")
        return await pitr.recover_to_timestamp(
            self._repo,
            self._event_source,
            timestamp,
            event_sink=self._event_sink,
            canon=self._canon,
            read_models=self._read_models,
            assets=self._assets,
            projector=projector,
            dry_run=dry_run,
            require_assets=require_assets,
        )

    # -- retention + reporting ---------------------------------------------- #

    async def gc(self) -> retention.GCPlan:
        """Apply the retention policy to the fleet."""
        return await retention.run_gc(self._repo, self._config, now=self._clock())

    async def health(self) -> BackupHealth:
        """Build the backup-fleet health report."""
        return await accounting.health_report(
            self._repo,
            self._config,
            now=self._clock(),
            event_source=self._event_source,
        )

    async def rpo_rto(self, *, restore_duration_s: float) -> RPORTOReport | None:
        """RPO/RTO accounting for the freshest recoverable point right now.

        Returns ``None`` if no backup exists. The recovery point is the freshest
        backup's pin; the source head is the live log head.
        """
        latest = await self._latest_backup()
        if latest is None:
            return None
        head = await self._event_source.head_position()
        rp = latest.descriptor.pinned_position
        rp_time = await accounting._position_time(self._event_source, rp)  # noqa: SLF001
        head_time = await accounting._position_time(self._event_source, head)  # noqa: SLF001
        return accounting.rpo_rto_report(
            recovery_point=rp,
            source_head=head,
            recovery_point_time=rp_time if rp_time is not None else 0.0,
            source_head_time=head_time if head_time is not None else 0.0,
            restore_duration_s=restore_duration_s,
            config=self._config,
        )

    @property
    def config(self) -> DRConfig:
        """The active configuration."""
        return self._config


__all__ = ["DRService"]
