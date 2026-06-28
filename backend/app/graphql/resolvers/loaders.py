"""Per-request dataloaders that batch DB reads to eliminate resolver N+1.

Registers the named loaders the field resolvers use:

* ``book`` — batch-load ``Book`` rows by id (for ``Shot.book``);
* ``book_owned`` — same, but filtered to the requesting key's owner (the
  ownership boundary), so a ``node``/relation lookup never crosses tenants.

A loader is built lazily on first use (``DataLoaderRegistry``) and lives only for
the request, so it batches within one execution without leaking across requests.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from sqlalchemy import select

from app.db.models.book import Book
from app.graphql.dataloader import DataLoader

if TYPE_CHECKING:
    from app.graphql.context import GraphQLContext


def register_loaders(ctx: GraphQLContext) -> None:
    """Register the domain dataloaders on a fresh request context."""

    def _book_loader() -> DataLoader[object, object]:
        async def batch(keys: Sequence[object]) -> list[object | None]:
            ids = [str(k) for k in keys]
            async with ctx.container.session_factory() as session:
                rows = (
                    (await session.execute(select(Book).where(Book.id.in_(ids))))
                    .scalars()
                    .all()
                )
            by_id = {row.id: row for row in rows}
            # Enforce the ownership boundary: a row owned by someone else is a miss.
            return [
                row if (row := by_id.get(i)) is not None and row.user_id == ctx.user_id else None
                for i in ids
            ]

        return DataLoader(batch)

    ctx.loaders.register("book", _book_loader)


__all__ = ["register_loaders"]
