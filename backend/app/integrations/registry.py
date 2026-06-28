"""The connector registry — name → connector, with a default population.

The service/API resolve a provider name (``"readwise"``, ``"notion"``, …) to a
:class:`~app.integrations.connector.SourceConnector` through a
:class:`ConnectorRegistry`. The registry stores connector *classes* (they are
cheap, stateless, and constructed per use) and exposes their static
:class:`~app.integrations.connector.ConnectorInfo` for the connect/health UI.
"""

from __future__ import annotations

from app.integrations.connector import ConnectorInfo, SourceConnector
from app.integrations.errors import ConfigurationError


class ConnectorRegistry:
    """A name → connector-class map."""

    def __init__(self) -> None:
        self._by_name: dict[str, type[SourceConnector]] = {}

    def register(self, connector_cls: type[SourceConnector]) -> ConnectorRegistry:
        """Register a connector class under its declared ``info().name``."""
        name = connector_cls.info().name
        if name in self._by_name and self._by_name[name] is not connector_cls:
            raise ConfigurationError(f"connector name already registered: {name!r}")
        self._by_name[name] = connector_cls
        return self

    def get(self, name: str) -> SourceConnector:
        """Construct the connector registered under ``name``."""
        cls = self._by_name.get(name)
        if cls is None:
            raise ConfigurationError(f"unknown connector: {name!r}")
        return cls()

    def has(self, name: str) -> bool:
        """Whether a connector is registered under ``name``."""
        return name in self._by_name

    def info(self, name: str) -> ConnectorInfo:
        """The static descriptor for ``name``."""
        cls = self._by_name.get(name)
        if cls is None:
            raise ConfigurationError(f"unknown connector: {name!r}")
        return cls.info()

    def all_info(self) -> list[ConnectorInfo]:
        """Every registered connector's descriptor, name-sorted (UI listing)."""
        return [cls.info() for _, cls in sorted(self._by_name.items())]

    def names(self) -> list[str]:
        """All registered connector names, sorted."""
        return sorted(self._by_name)


def default_registry() -> ConnectorRegistry:
    """A registry pre-populated with every built-in source connector."""
    from app.integrations.connectors import (
        KindleClippingsConnector,
        NotionConnector,
        PocketConnector,
        ReadwiseConnector,
        RssConnector,
        WebArticleConnector,
    )

    registry = ConnectorRegistry()
    for cls in (
        ReadwiseConnector,
        KindleClippingsConnector,
        NotionConnector,
        RssConnector,
        PocketConnector,
        WebArticleConnector,
    ):
        registry.register(cls)
    return registry


__all__ = ["ConnectorRegistry", "default_registry"]
