"""The per-request GraphQL execution context.

Every resolver receives this as its third argument. It bundles the wired
:class:`~app.composition.Container`, the authenticated :class:`ApiKeyRecord`, the
per-request :class:`~app.graphql.dataloader.DataLoaderRegistry` (with the domain
loaders registered), and convenience helpers (``require``-scope, object-store URL
presigning). It is constructed fresh per request, so the dataloaders never serve
stale rows across requests.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from app.graphql.auth import ApiKeyRecord, require_scope
from app.graphql.dataloader import DataLoader, DataLoaderRegistry

if TYPE_CHECKING:
    from app.composition import Container


@dataclass
class GraphQLContext:
    """What a resolver sees: the container, the API key, and per-request loaders."""

    container: Container
    api_key: ApiKeyRecord
    loaders: DataLoaderRegistry = field(default_factory=DataLoaderRegistry)

    def require(self, scope: str) -> None:
        """Raise ``FORBIDDEN`` unless this request's key holds ``scope``."""
        require_scope(self.api_key, scope)

    @property
    def user_id(self) -> str:
        """The user that owns the presenting API key (the ownership boundary)."""
        return self.api_key.user_id

    def loader(self, name: str) -> DataLoader[Any, Any]:
        return self.loaders.get(name)

    def presign(self, key: str | None) -> str | None:
        """Presign an object-store key into an ephemeral GET URL (``None`` passthrough)."""
        if not key:
            return None
        return self.container.object_store.presigned_get_url(key)


def build_context(container: Container, api_key: ApiKeyRecord) -> GraphQLContext:
    """Construct a request context with the domain dataloaders registered."""
    from app.graphql.resolvers.loaders import register_loaders

    ctx = GraphQLContext(container=container, api_key=api_key)
    register_loaders(ctx)
    return ctx


__all__ = ["GraphQLContext", "build_context"]
