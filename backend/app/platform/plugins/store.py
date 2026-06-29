"""Async Postgres persistence for the registry, installations, reviews, ratings.

Repositories follow the project convention
(:class:`~app.db.repositories.base.BaseRepository`): they hold an
:class:`AsyncSession` and *flush* (never commit) so the unit-of-work boundary
owns the transaction. They translate between the serialized JSONB rows and the
pure platform value types (:class:`PluginManifest`, :class:`PluginInstallation`,
:class:`RatingStats`, ...), so callers always work with validated objects.

Three repositories:

* :class:`RegistryStore` — publish (idempotent on content digest), fetch by ref,
  list the catalog, set review status, bump install/rating counters.
* :class:`InstallationStore` — load/save a tenant's :class:`PluginInstallation`
  (the lifecycle row), list active installs (for hook hydration), and resolve
  the set of :class:`AvailablePlugin` the dependency resolver needs.
* :class:`RatingStore` — upsert a user's rating (UNIQUE per (plugin, user)),
  returning the delta so the registry counters stay correct.

Audit rows are appended on every mutation via :class:`AuditStore`.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select

from app.db.base import new_id
from app.db.repositories.base import BaseRepository
from app.platform.plugins.db_models import (
    PluginAuditRow,
    PluginInstallationRow,
    PluginRatingRow,
    PluginRegistryEntry,
    PluginReviewRow,
)
from app.platform.plugins.errors import PluginNotFoundError, RegistryError
from app.platform.plugins.lifecycle import (
    PluginInstallation,
    PluginState,
    VersionRecord,
)
from app.platform.plugins.manifest import PluginManifest
from app.platform.plugins.marketplace import RatingStats, ReviewStatus
from app.platform.plugins.resolver import AvailablePlugin
from app.platform.plugins.signing import Signature
from app.platform.plugins.version import Version

# --------------------------------------------------------------------------- #
# Registry / marketplace
# --------------------------------------------------------------------------- #


class RegistryStore(BaseRepository):
    """CRUD for published artifacts (the marketplace catalog)."""

    async def publish(
        self,
        *,
        manifest: PluginManifest,
        source: str,
        digest: str,
        status: ReviewStatus,
        signature: Signature | None,
    ) -> PluginRegistryEntry:
        """Insert a new artifact row; idempotent on the content ``digest``.

        Re-publishing identical bytes (same digest) returns the existing row.
        Publishing a *different* artifact under an already-used (id, version) is
        rejected — versions are immutable once published.
        """
        existing = await self._by_digest(digest)
        if existing is not None:
            return existing
        clash = await self._by_ref(manifest.id, str(manifest.version))
        if clash is not None:
            raise RegistryError(
                f"{manifest.ref} already published with different content; bump the version"
            )
        row = PluginRegistryEntry(
            id=new_id(),
            plugin_id=manifest.id,
            version=str(manifest.version),
            name=manifest.name,
            publisher=manifest.publisher,
            digest=digest,
            status=status.value,
            max_risk=manifest.max_risk.value,
            yanked=status is ReviewStatus.YANKED,
            signed=signature is not None,
            manifest=manifest.to_dict(),
            source=source,
            signature=signature.to_dict() if signature else None,
            description=manifest.description,
        )
        self.session.add(row)
        await self.session.flush()
        return row

    async def get(self, plugin_id: str, version: str) -> PluginRegistryEntry:
        row = await self._by_ref(plugin_id, version)
        if row is None:
            raise PluginNotFoundError(f"{plugin_id}@{version} is not published")
        return row

    async def get_manifest(self, plugin_id: str, version: str) -> PluginManifest:
        row = await self.get(plugin_id, version)
        return PluginManifest.parse(row.manifest)

    async def latest_approved(self, plugin_id: str) -> PluginRegistryEntry | None:
        """The highest-version APPROVED, non-yanked artifact for ``plugin_id``."""
        rows = await self._versions(plugin_id, only_installable=True)
        if not rows:
            return None
        return max(rows, key=lambda r: Version.parse(r.version))

    async def set_status(
        self, plugin_id: str, version: str, status: ReviewStatus
    ) -> PluginRegistryEntry:
        row = await self.get(plugin_id, version)
        row.status = status.value
        row.yanked = status is ReviewStatus.YANKED
        await self.session.flush()
        return row

    async def bump_install_count(self, plugin_id: str, version: str, *, delta: int = 1) -> None:
        row = await self.get(plugin_id, version)
        row.install_count = max(0, row.install_count + delta)
        await self.session.flush()

    async def apply_rating_delta(
        self, plugin_id: str, *, count_delta: int, sum_delta: int
    ) -> RatingStats:
        """Apply a rating delta across the plugin's versions and return the stats.

        The aggregate is stored on the latest row to keep the listing read cheap;
        all versions of a plugin share one rating signal.
        """
        rows = await self._versions(plugin_id, only_installable=False)
        if not rows:
            raise PluginNotFoundError(f"{plugin_id} has no published versions")
        target = max(rows, key=lambda r: Version.parse(r.version))
        target.rating_count = max(0, target.rating_count + count_delta)
        target.rating_sum = max(0, target.rating_sum + sum_delta)
        await self.session.flush()
        return RatingStats(count=target.rating_count, total=target.rating_sum)

    async def list_catalog(
        self, *, include_pending: bool = False, limit: int = 100, offset: int = 0
    ) -> list[PluginRegistryEntry]:
        """List the latest version of each plugin (newest publish first)."""
        stmt = select(PluginRegistryEntry)
        if not include_pending:
            stmt = stmt.where(
                PluginRegistryEntry.status == ReviewStatus.APPROVED.value,
                PluginRegistryEntry.yanked.is_(False),
            )
        stmt = stmt.order_by(PluginRegistryEntry.created_at.desc()).limit(limit).offset(offset)
        rows = (await self.session.execute(stmt)).scalars().all()
        # Keep only the highest version per plugin id.
        latest: dict[str, PluginRegistryEntry] = {}
        for r in rows:
            cur = latest.get(r.plugin_id)
            if cur is None or Version.parse(r.version) > Version.parse(cur.version):
                latest[r.plugin_id] = r
        return sorted(latest.values(), key=lambda r: r.created_at, reverse=True)

    async def available_plugins(self) -> list[AvailablePlugin]:
        """Every installable artifact as an :class:`AvailablePlugin` for resolution."""
        rows = (
            (
                await self.session.execute(
                    select(PluginRegistryEntry).where(
                        PluginRegistryEntry.status == ReviewStatus.APPROVED.value,
                        PluginRegistryEntry.yanked.is_(False),
                    )
                )
            )
            .scalars()
            .all()
        )
        out: list[AvailablePlugin] = []
        for r in rows:
            manifest = PluginManifest.parse(r.manifest)
            out.append(
                AvailablePlugin(
                    id=r.plugin_id,
                    version=Version.parse(r.version),
                    dependencies=manifest.dependencies,
                    enabled=True,
                )
            )
        return out

    # -- private --------------------------------------------------------- #

    async def _by_ref(self, plugin_id: str, version: str) -> PluginRegistryEntry | None:
        return (
            await self.session.execute(
                select(PluginRegistryEntry).where(
                    PluginRegistryEntry.plugin_id == plugin_id,
                    PluginRegistryEntry.version == version,
                )
            )
        ).scalar_one_or_none()

    async def _by_digest(self, digest: str) -> PluginRegistryEntry | None:
        return (
            await self.session.execute(
                select(PluginRegistryEntry).where(PluginRegistryEntry.digest == digest)
            )
        ).scalar_one_or_none()

    async def _versions(
        self, plugin_id: str, *, only_installable: bool
    ) -> list[PluginRegistryEntry]:
        stmt = select(PluginRegistryEntry).where(PluginRegistryEntry.plugin_id == plugin_id)
        if only_installable:
            stmt = stmt.where(
                PluginRegistryEntry.status == ReviewStatus.APPROVED.value,
                PluginRegistryEntry.yanked.is_(False),
            )
        return list((await self.session.execute(stmt)).scalars().all())


# --------------------------------------------------------------------------- #
# Installations (lifecycle)
# --------------------------------------------------------------------------- #


class InstallationStore(BaseRepository):
    """Load/save per-tenant :class:`PluginInstallation` lifecycle rows."""

    async def get(self, owner: str, plugin_id: str) -> PluginInstallation | None:
        row = await self._row(owner, plugin_id)
        return _installation_from_row(row) if row is not None else None

    async def save(
        self, owner: str, installation: PluginInstallation, *, granted: list[str]
    ) -> PluginInstallation:
        """Upsert the installation row from a pure :class:`PluginInstallation`."""
        row = await self._row(owner, installation.plugin_id)
        if row is None:
            row = PluginInstallationRow(id=new_id(), owner=owner, plugin_id=installation.plugin_id)
            self.session.add(row)
        row.version = str(installation.version)
        row.state = installation.state.value
        row.failure_count = installation.failure_count
        row.granted = list(granted)
        row.history = [r.to_dict() for r in installation.history]
        await self.session.flush()
        return installation

    async def granted_capabilities(self, owner: str, plugin_id: str) -> list[str]:
        row = await self._row(owner, plugin_id)
        return list(row.granted) if row is not None else []

    async def list_active(self, owner: str) -> list[PluginInstallation]:
        rows = (
            (
                await self.session.execute(
                    select(PluginInstallationRow).where(
                        PluginInstallationRow.owner == owner,
                        PluginInstallationRow.state == PluginState.ENABLED.value,
                    )
                )
            )
            .scalars()
            .all()
        )
        return [_installation_from_row(r) for r in rows]

    async def list_all(self, owner: str) -> list[PluginInstallation]:
        rows = (
            (
                await self.session.execute(
                    select(PluginInstallationRow).where(PluginInstallationRow.owner == owner)
                )
            )
            .scalars()
            .all()
        )
        return [_installation_from_row(r) for r in rows]

    async def available_for_owner(self, owner: str) -> list[AvailablePlugin]:
        """The owner's installed plugins as resolver inputs (enabled = active)."""
        rows = (
            (
                await self.session.execute(
                    select(PluginInstallationRow).where(PluginInstallationRow.owner == owner)
                )
            )
            .scalars()
            .all()
        )
        return [
            AvailablePlugin(
                id=r.plugin_id,
                version=Version.parse(r.version),
                dependencies=(),  # filled by the service from the registry manifest
                enabled=r.state == PluginState.ENABLED.value,
            )
            for r in rows
        ]

    async def _row(self, owner: str, plugin_id: str) -> PluginInstallationRow | None:
        return (
            await self.session.execute(
                select(PluginInstallationRow).where(
                    PluginInstallationRow.owner == owner,
                    PluginInstallationRow.plugin_id == plugin_id,
                )
            )
        ).scalar_one_or_none()


def _installation_from_row(row: PluginInstallationRow) -> PluginInstallation:
    history = tuple(
        VersionRecord(version=Version.parse(h["version"]), at=_parse_dt(h["at"]))
        for h in (row.history or [])
    )
    return PluginInstallation(
        plugin_id=row.plugin_id,
        version=Version.parse(row.version),
        state=PluginState(row.state),
        failure_count=row.failure_count,
        history=history,
        updated_at=row.updated_at,
    )


def _parse_dt(value: str) -> Any:
    from datetime import datetime

    return datetime.fromisoformat(value)


# --------------------------------------------------------------------------- #
# Ratings + reviews + audit
# --------------------------------------------------------------------------- #


class RatingStore(BaseRepository):
    """Upsert a user's rating; returns the (count_delta, sum_delta) for the registry."""

    async def upsert(
        self, *, plugin_id: str, user_id: str, stars: int, review: str = ""
    ) -> tuple[int, int]:
        if not 1 <= stars <= 5:
            raise RegistryError("stars must be 1..5")
        existing = (
            await self.session.execute(
                select(PluginRatingRow).where(
                    PluginRatingRow.plugin_id == plugin_id,
                    PluginRatingRow.user_id == user_id,
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            self.session.add(
                PluginRatingRow(
                    id=new_id(),
                    plugin_id=plugin_id,
                    user_id=user_id,
                    stars=stars,
                    review=review,
                )
            )
            await self.session.flush()
            return (1, stars)
        old = existing.stars
        existing.stars = stars
        existing.review = review
        await self.session.flush()
        return (0, stars - old)

    async def aggregate(self, plugin_id: str) -> RatingStats:
        row = (
            await self.session.execute(
                select(
                    func.count(PluginRatingRow.id),
                    func.coalesce(func.sum(PluginRatingRow.stars), 0),
                ).where(PluginRatingRow.plugin_id == plugin_id)
            )
        ).one()
        return RatingStats(count=int(row[0]), total=int(row[1]))


class ReviewStore(BaseRepository):
    """Append-only moderation decisions."""

    async def record(
        self,
        *,
        plugin_id: str,
        version: str,
        decision: str,
        reviewer: str | None,
        notes: str = "",
    ) -> PluginReviewRow:
        row = PluginReviewRow(
            id=new_id(),
            plugin_id=plugin_id,
            version=version,
            decision=decision,
            reviewer=reviewer,
            notes=notes,
        )
        self.session.add(row)
        await self.session.flush()
        return row

    async def history(self, plugin_id: str, version: str) -> list[PluginReviewRow]:
        return list(
            (
                await self.session.execute(
                    select(PluginReviewRow)
                    .where(
                        PluginReviewRow.plugin_id == plugin_id,
                        PluginReviewRow.version == version,
                    )
                    .order_by(PluginReviewRow.created_at)
                )
            )
            .scalars()
            .all()
        )


class AuditStore(BaseRepository):
    """Append-only lifecycle/registry audit log."""

    async def record(
        self,
        *,
        plugin_id: str,
        action: str,
        actor: str | None,
        summary: str = "",
        detail: dict[str, Any] | None = None,
    ) -> None:
        self.session.add(
            PluginAuditRow(
                id=new_id(),
                plugin_id=plugin_id,
                action=action,
                actor=actor,
                summary=summary,
                detail=detail,
            )
        )
        await self.session.flush()

    async def for_plugin(self, plugin_id: str, *, limit: int = 100) -> list[PluginAuditRow]:
        return list(
            (
                await self.session.execute(
                    select(PluginAuditRow)
                    .where(PluginAuditRow.plugin_id == plugin_id)
                    .order_by(PluginAuditRow.created_at.desc())
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )


__all__ = [
    "AuditStore",
    "InstallationStore",
    "RatingStore",
    "RegistryStore",
    "ReviewStore",
]
