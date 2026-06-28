"""DB-backed repositories + the async ``RecommendationService``.

This is the thin, I/O-bearing shell around the pure
:class:`~app.recommendations.engine.RecommendationEngine`. It:

* reads the warehouse tables (``book_interactions`` / ``book_features``) into the
  plain value objects the engine consumes,
* runs the pure pipeline,
* and writes events back (``log_interaction``) + refreshes the cached per-user
  taste vector (``user_taste_vectors``).

The service is deliberately scoped: it loads a *neighbourhood* of the
interaction log (the target user's events + the readers who co-engaged with the
user's books) rather than the entire log, so CF stays bounded as the corpus
grows — the same "recall the relevant slice, never the whole book" discipline
the canon layer uses (§8.4). All DB access goes through repositories that flush
but never commit; the unit-of-work boundary owns the transaction.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.recommendation import (
    BookFeatureRow,
    BookInteraction,
    UserTasteVector,
)
from app.db.repositories.base import BaseRepository

from .engine import RecommendationEngine
from .taste import TasteAccumulator, TasteModel
from .types import (
    BookFeatures,
    Interaction,
    InteractionKind,
    Recommendation,
    RecsConfig,
)

# --------------------------------------------------------------------------- #
# Repositories
# --------------------------------------------------------------------------- #


def _to_interaction(row: BookInteraction) -> Interaction:
    """Map a warehouse row to the engine's value object (kind string → enum)."""
    try:
        kind = InteractionKind(row.kind)
    except ValueError:
        kind = InteractionKind.VIEW  # fail-soft on an unknown stored kind
    return Interaction(
        user_id=row.user_id,
        book_id=row.book_id,
        kind=kind,
        at=row.created_at,
        weight=row.weight,
        dwell_s=row.dwell_s,
    )


def _to_features(
    row: BookFeatureRow, *, title: str = "", author: str | None = None
) -> BookFeatures:
    return BookFeatures(
        book_id=row.book_id,
        title=title,
        author=author,
        embedding=list(row.embedding) if row.embedding is not None else [],
        tags=tuple(row.tags or ()),
        popularity=row.popularity,
    )


class InteractionRepo(BaseRepository):
    """CRUD + neighbourhood reads over ``book_interactions``."""

    async def log(
        self,
        *,
        user_id: str,
        book_id: str,
        kind: InteractionKind,
        weight: float | None = None,
        dwell_s: float | None = None,
        at: datetime | None = None,
    ) -> BookInteraction:
        """Append one interaction event."""
        row = BookInteraction(
            user_id=user_id,
            book_id=book_id,
            kind=kind.value,
            weight=weight,
            dwell_s=dwell_s,
        )
        if at is not None:
            row.created_at = at
        self.session.add(row)
        await self.session.flush()
        return row

    async def for_user(self, user_id: str, *, limit: int = 1000) -> list[Interaction]:
        """The user's own most-recent events (newest first, then chronological)."""
        stmt = (
            select(BookInteraction)
            .where(BookInteraction.user_id == user_id)
            .order_by(BookInteraction.created_at.desc())
            .limit(limit)
        )
        rows = list((await self.session.execute(stmt)).scalars().all())
        return [_to_interaction(r) for r in reversed(rows)]

    async def neighbourhood(
        self,
        user_id: str,
        *,
        book_limit: int = 50,
        user_limit: int = 500,
        event_limit: int = 20_000,
    ) -> list[Interaction]:
        """The CF-relevant two-hop slice around the target user.

        Item-item CF recommends a book ``B`` because a co-reader engaged with both
        ``B`` and a book the user read — so the relevant slice is a **two-hop**
        expansion, not just events on the user's own books:

        1. the books the user engaged with (hop 0),
        2. the *co-readers* who also engaged with any of those books (hop 1),
        3. **all** events by those co-readers (hop 2) — which surfaces the new
           books ``B`` the CF models propose.

        Bounded by ``book_limit`` seed books, ``user_limit`` co-readers, and
        ``event_limit`` rows so the slice stays a bounded "recall the relevant
        neighbourhood, never the whole log" read (§8.4). Always a superset of the
        user's own events.
        """
        user_books_stmt = (
            select(BookInteraction.book_id)
            .where(BookInteraction.user_id == user_id)
            .distinct()
            .limit(book_limit)
        )
        book_ids = list((await self.session.execute(user_books_stmt)).scalars().all())
        if not book_ids:
            return await self.for_user(user_id)

        coreaders_stmt = (
            select(BookInteraction.user_id)
            .where(BookInteraction.book_id.in_(book_ids))
            .distinct()
            .limit(user_limit)
        )
        user_ids = set((await self.session.execute(coreaders_stmt)).scalars().all())
        user_ids.add(user_id)

        stmt = (
            select(BookInteraction)
            .where(BookInteraction.user_id.in_(user_ids))
            .order_by(BookInteraction.created_at.desc())
            .limit(event_limit)
        )
        rows = list((await self.session.execute(stmt)).scalars().all())
        # Guarantee the user's own events are present even if truncated above.
        own = await self.for_user(user_id)
        seen = {(r.user_id, r.book_id, r.created_at) for r in rows}
        merged = [_to_interaction(r) for r in rows]
        for ev in own:
            if (ev.user_id, ev.book_id, ev.at) not in seen:
                merged.append(ev)
        return merged

    async def engagement_counts(self, book_ids: Sequence[str] | None = None) -> dict[str, float]:
        """Net positive engagement count per book (for the popularity backfill)."""
        stmt = select(BookInteraction.book_id)
        if book_ids:
            stmt = stmt.where(BookInteraction.book_id.in_(list(book_ids)))
        rows = (await self.session.execute(stmt)).scalars().all()
        counts: dict[str, float] = {}
        for book_id in rows:
            counts[book_id] = counts.get(book_id, 0.0) + 1.0
        return counts


class BookFeatureRepo(BaseRepository):
    """Upsert + bulk read over ``book_features`` (the content feature corpus)."""

    async def upsert(
        self,
        *,
        book_id: str,
        embedding: list[float] | None = None,
        popularity: float | None = None,
        tags: list[str] | None = None,
    ) -> None:
        """Insert-or-update a book's feature row (idempotent on ``book_id``)."""
        values: dict[str, object] = {"book_id": book_id}
        if embedding is not None:
            values["embedding"] = embedding
        if popularity is not None:
            values["popularity"] = popularity
        if tags is not None:
            values["tags"] = tags
        update_cols = {k: v for k, v in values.items() if k != "book_id"}
        stmt = pg_insert(BookFeatureRow).values(**values)
        if update_cols:
            stmt = stmt.on_conflict_do_update(
                index_elements=[BookFeatureRow.book_id], set_=update_cols
            )
        else:
            stmt = stmt.on_conflict_do_nothing(index_elements=[BookFeatureRow.book_id])
        await self.session.execute(stmt)
        await self.session.flush()

    async def corpus(self, *, limit: int = 5000) -> dict[str, BookFeatures]:
        """Load the full recommendable feature corpus, joined to book metadata."""
        from app.db.models.book import Book

        stmt = (
            select(BookFeatureRow, Book.title, Book.author)
            .join(Book, Book.id == BookFeatureRow.book_id)
            .limit(limit)
        )
        out: dict[str, BookFeatures] = {}
        for row, title, author in (await self.session.execute(stmt)).all():
            out[row.book_id] = _to_features(row, title=title or "", author=author)
        return out


class TasteVectorRepo(BaseRepository):
    """Read/write the cached per-user taste accumulator (``user_taste_vectors``)."""

    async def get(self, user_id: str) -> TasteAccumulator | None:
        """Load the cached accumulator for a user (``None`` if never built)."""
        row = (
            (
                await self.session.execute(
                    select(UserTasteVector).where(UserTasteVector.user_id == user_id)
                )
            )
            .scalars()
            .first()
        )
        if row is None:
            return None
        return TasteAccumulator(
            sum_vec=list(row.sum_vec) if row.sum_vec is not None else [],
            weight_total=row.weight_total,
            as_of=row.last_event_at,
            event_count=row.event_count,
        )

    async def upsert(self, user_id: str, acc: TasteAccumulator) -> None:
        """Persist a folded accumulator (idempotent on ``user_id``)."""
        values = {
            "user_id": user_id,
            "sum_vec": acc.sum_vec or None,
            "weight_total": acc.weight_total,
            "last_event_at": acc.as_of,
            "event_count": acc.event_count,
            "refreshed_at": datetime.now(UTC),
        }
        stmt = pg_insert(UserTasteVector).values(**values)
        stmt = stmt.on_conflict_do_update(
            index_elements=[UserTasteVector.user_id],
            set_={k: v for k, v in values.items() if k != "user_id"},
        )
        await self.session.execute(stmt)
        await self.session.flush()


# --------------------------------------------------------------------------- #
# The service
# --------------------------------------------------------------------------- #


class RecommendationService:
    """Async orchestrator: load the warehouse → run the engine → persist signals.

    Bound to an :class:`AsyncSession` per request (like the other Kinora
    services). The pure :class:`RecommendationEngine` holds all the logic; this
    shell only does I/O.
    """

    def __init__(self, session: AsyncSession, *, config: RecsConfig | None = None) -> None:
        self._session = session
        self._config = config or RecsConfig()
        self._engine = RecommendationEngine(self._config)
        self._interactions = InteractionRepo(session)
        self._features = BookFeatureRepo(session)
        self._taste = TasteVectorRepo(session)

    async def recommend(
        self,
        user_id: str,
        *,
        top_k: int | None = None,
        as_of: datetime | None = None,
    ) -> list[Recommendation]:
        """Recommend books for ``user_id`` from the warehouse."""
        now = as_of or datetime.now(UTC)
        interactions = await self._interactions.neighbourhood(user_id)
        features = await self._features.corpus()
        return self._engine.recommend(
            user_id,
            interactions=interactions,
            features=features,
            as_of=now,
            top_k=top_k,
        )

    async def log_interaction(
        self,
        *,
        user_id: str,
        book_id: str,
        kind: InteractionKind,
        weight: float | None = None,
        dwell_s: float | None = None,
        refresh_taste: bool = True,
    ) -> None:
        """Record an interaction and (optionally) fold it into the taste cache."""
        now = datetime.now(UTC)
        await self._interactions.log(
            user_id=user_id, book_id=book_id, kind=kind, weight=weight, dwell_s=dwell_s, at=now
        )
        if refresh_taste:
            await self._fold_taste(user_id, book_id, kind, weight, now)

    async def _fold_taste(
        self,
        user_id: str,
        book_id: str,
        kind: InteractionKind,
        weight: float | None,
        now: datetime,
    ) -> None:
        feature_row = (
            (
                await self._session.execute(
                    select(BookFeatureRow).where(BookFeatureRow.book_id == book_id)
                )
            )
            .scalars()
            .first()
        )
        if feature_row is None or feature_row.embedding is None:
            return  # no content vector → nothing to fold into the taste vector
        feat = _to_features(feature_row)
        prior = await self._taste.get(user_id) or TasteAccumulator()
        model = TasteModel(half_life_days=self._config.taste_half_life_days)
        event = Interaction(user_id, book_id, kind, now, weight=weight)
        folded = model.fold(prior, [event], {book_id: feat}, as_of=now)
        await self._taste.upsert(user_id, folded)

    async def backfill_features(
        self,
        *,
        book_id: str,
        embedding: list[float] | None = None,
        tags: list[str] | None = None,
    ) -> None:
        """Refresh a book's feature row + its popularity from the interaction log."""
        counts = await self._interactions.engagement_counts([book_id])
        await self._features.upsert(
            book_id=book_id,
            embedding=embedding,
            popularity=counts.get(book_id, 0.0),
            tags=tags,
        )


__all__ = [
    "BookFeatureRepo",
    "InteractionRepo",
    "RecommendationService",
    "TasteVectorRepo",
]
