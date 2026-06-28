"""Backfills + maintenance jobs (kinora.md §8.2, §8.7, §12).

Periodic / on-demand operational chores an operator runs against a live stack:

* ``census``           — a row count across every core table (a cheap health
  snapshot for the dashboard / capacity planning).
* ``stuck-imports``    — find books left ``importing`` and (optionally) respawn
  Phase-A recovery for them via the container's durable recovery path.
* ``cache-audit``      — the §8.7 shot-cache coverage: how many cache rows exist,
  how many carry a clip key, total cached video-seconds.
* ``embedding-coverage`` — the §8.2 episodic store readiness: fraction of
  accepted shots / entities that have an embedding (episodic search needs them).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import func, select

from app.cli.formatting import humanize_seconds, pct, truncate
from app.cli.output import Payload, Table, kv_table
from app.composition import Container
from app.db.models.book import Book, Page
from app.db.models.budget import BudgetLedger
from app.db.models.continuity import ContinuityState
from app.db.models.defect import Defect
from app.db.models.entity import Entity
from app.db.models.enums import BookStatus, ShotStatus
from app.db.models.render_job import RenderJob
from app.db.models.scene import Scene
from app.db.models.session import Session
from app.db.models.shot import Shot, ShotCache
from app.db.models.user import User

_CENSUS_MODELS: tuple[tuple[str, type], ...] = (
    ("users", User),
    ("books", Book),
    ("pages", Page),
    ("scenes", Scene),
    ("shots", Shot),
    ("shot_cache", ShotCache),
    ("entities", Entity),
    ("continuity_states", ContinuityState),
    ("sessions", Session),
    ("render_jobs", RenderJob),
    ("defects", Defect),
    ("budget_ledger", BudgetLedger),
)


@dataclass(frozen=True, slots=True)
class CensusReport:
    """The result of ``maint census`` — a row count per core table."""

    counts: dict[str, int]

    def render_payload(self) -> Payload:
        data = {"counts": self.counts, "total_rows": sum(self.counts.values())}
        table = Table(
            title="table census",
            columns=("table", "rows"),
            rows=[(name, str(count)) for name, count in self.counts.items()],
        )
        return Payload.of(data, table)


@dataclass(frozen=True, slots=True)
class StuckImportRow:
    book_id: str
    title: str
    has_source: bool


@dataclass(frozen=True, slots=True)
class StuckImportReport:
    """The result of ``maint stuck-imports``."""

    books: tuple[StuckImportRow, ...]
    spawned: int | None = None  # None when not respawning (report-only)

    def render_payload(self) -> Payload:
        data = {
            "count": len(self.books),
            "spawned": self.spawned,
            "books": [
                {"book_id": b.book_id, "title": b.title, "has_source": b.has_source}
                for b in self.books
            ],
        }
        table = Table(
            title=f"stuck imports ({len(self.books)})"
            + (f" — respawned {self.spawned}" if self.spawned is not None else ""),
            columns=("book_id", "title", "has_source_pdf"),
            rows=[
                (b.book_id, truncate(b.title, 40), "yes" if b.has_source else "NO")
                for b in self.books
            ],
        )
        return Payload.of(data, table)


@dataclass(frozen=True, slots=True)
class CacheAuditReport:
    """The result of ``maint cache-audit`` — §8.7 shot-cache coverage."""

    total_rows: int
    with_clip: int
    without_clip: int
    cached_video_seconds: float

    def render_payload(self) -> Payload:
        data = {
            "total_rows": self.total_rows,
            "with_clip": self.with_clip,
            "without_clip": self.without_clip,
            "clip_coverage_pct": ((self.with_clip / self.total_rows) if self.total_rows else None),
            "cached_video_seconds": self.cached_video_seconds,
        }
        table = kv_table(
            "shot-cache audit (§8.7)",
            {
                "cache_rows": self.total_rows,
                "with_clip": f"{self.with_clip} ({pct(self.with_clip, self.total_rows)})",
                "without_clip": self.without_clip,
                "cached_footage": humanize_seconds(self.cached_video_seconds),
            },
        )
        return Payload.of(data, table)


@dataclass(frozen=True, slots=True)
class EmbeddingCoverageReport:
    """The result of ``maint embedding-coverage`` — §8.2 episodic readiness."""

    accepted_shots: int
    accepted_with_embedding: int
    entities: int
    entities_with_embedding: int
    scope: dict[str, str | None] = field(default_factory=dict)

    def render_payload(self) -> Payload:
        data = {
            "scope": self.scope,
            "accepted_shots": self.accepted_shots,
            "accepted_with_embedding": self.accepted_with_embedding,
            "shot_embedding_pct": (
                (self.accepted_with_embedding / self.accepted_shots)
                if self.accepted_shots
                else None
            ),
            "entities": self.entities,
            "entities_with_embedding": self.entities_with_embedding,
            "entity_embedding_pct": (
                (self.entities_with_embedding / self.entities) if self.entities else None
            ),
        }
        table = kv_table(
            "embedding coverage (§8.2)",
            {
                "accepted_shots": self.accepted_shots,
                "shots_with_embedding": (
                    f"{self.accepted_with_embedding} "
                    f"({pct(self.accepted_with_embedding, self.accepted_shots)})"
                ),
                "entities": self.entities,
                "entities_with_embedding": (
                    f"{self.entities_with_embedding} "
                    f"({pct(self.entities_with_embedding, self.entities)})"
                ),
            },
        )
        return Payload.of(data, table)


# --------------------------------------------------------------------------- #
# Actions
# --------------------------------------------------------------------------- #


async def census(container: Container) -> CensusReport:
    """Count rows in every core table (cheap capacity snapshot)."""
    counts: dict[str, int] = {}
    async with container.session_factory() as db:
        for name, model in _CENSUS_MODELS:
            value = (await db.execute(select(func.count()).select_from(model))).scalar_one()
            counts[name] = int(value)
    return CensusReport(counts=counts)


async def stuck_imports(
    container: Container, *, respawn: bool = False, limit: int = 50
) -> StuckImportReport:
    """Find books stuck ``importing``; optionally respawn durable recovery.

    Report-only by default. With ``respawn=True`` it calls the container's
    :meth:`recover_importing_books`, which reloads each book's persisted source
    PDF and re-runs ingest under the shared single-flight lock (so it never
    double-ingests a book already in flight).
    """
    async with container.session_factory() as db:
        rows = list(
            (
                await db.execute(
                    select(Book)
                    .where(Book.status == BookStatus.IMPORTING)
                    .order_by(Book.created_at.asc())
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
    books = tuple(
        StuckImportRow(book_id=b.id, title=b.title, has_source=bool(b.source_pdf_key)) for b in rows
    )
    spawned: int | None = None
    if respawn:
        spawned = await container.recover_importing_books(limit=limit)
    return StuckImportReport(books=books, spawned=spawned)


async def cache_audit(container: Container, *, book_id: str | None = None) -> CacheAuditReport:
    """The §8.7 shot-cache coverage: rows, clip coverage, cached seconds."""
    async with container.session_factory() as db:
        base = select(func.count()).select_from(ShotCache)
        clip = select(func.count()).select_from(ShotCache).where(ShotCache.clip_key.is_not(None))
        secs = select(func.coalesce(func.sum(ShotCache.video_seconds), 0.0))
        if book_id is not None:
            base = base.where(ShotCache.book_id == book_id)
            clip = clip.where(ShotCache.book_id == book_id)
            secs = secs.where(ShotCache.book_id == book_id)
        total = int((await db.execute(base)).scalar_one())
        with_clip = int((await db.execute(clip)).scalar_one())
        cached_seconds = float((await db.execute(secs)).scalar_one() or 0.0)
    return CacheAuditReport(
        total_rows=total,
        with_clip=with_clip,
        without_clip=total - with_clip,
        cached_video_seconds=cached_seconds,
    )


async def embedding_coverage(
    container: Container, *, book_id: str | None = None
) -> EmbeddingCoverageReport:
    """The §8.2 episodic-store readiness: embedding coverage of shots + entities."""
    async with container.session_factory() as db:
        accepted = select(func.count()).select_from(Shot).where(Shot.status == ShotStatus.ACCEPTED)
        accepted_emb = (
            select(func.count())
            .select_from(Shot)
            .where(Shot.status == ShotStatus.ACCEPTED, Shot.embedding.is_not(None))
        )
        ent = select(func.count()).select_from(Entity)
        ent_emb = select(func.count()).select_from(Entity).where(Entity.embedding.is_not(None))
        if book_id is not None:
            accepted = accepted.where(Shot.book_id == book_id)
            accepted_emb = accepted_emb.where(Shot.book_id == book_id)
            ent = ent.where(Entity.book_id == book_id)
            ent_emb = ent_emb.where(Entity.book_id == book_id)
        accepted_shots = int((await db.execute(accepted)).scalar_one())
        accepted_with_embedding = int((await db.execute(accepted_emb)).scalar_one())
        entities = int((await db.execute(ent)).scalar_one())
        entities_with_embedding = int((await db.execute(ent_emb)).scalar_one())
    return EmbeddingCoverageReport(
        accepted_shots=accepted_shots,
        accepted_with_embedding=accepted_with_embedding,
        entities=entities,
        entities_with_embedding=entities_with_embedding,
        scope={"book_id": book_id},
    )


__all__ = [
    "CacheAuditReport",
    "CensusReport",
    "EmbeddingCoverageReport",
    "StuckImportReport",
    "StuckImportRow",
    "cache_audit",
    "census",
    "embedding_coverage",
    "stuck_imports",
]
