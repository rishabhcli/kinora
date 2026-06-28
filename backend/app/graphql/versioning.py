"""The public API's version + deprecation policy — the single source of truth.

A public API needs an explicit contract about what is stable and what is going
away. This module owns:

* :data:`API_VERSION` — the gateway's semantic version (independent of the
  backend's internal ``app.__version__``);
* :data:`SCHEMA_STABILITY` — the lifecycle label (``stable``/``beta``);
* :data:`DEPRECATIONS` — the registry of deprecated schema members, each with a
  human reason, the version it was deprecated in, and a planned removal version.

``Query.apiVersion`` resolves to :func:`api_version_payload`, so a client can
discover the live version + deprecation list at runtime; the SDL printer and
introspection surface the same ``deprecationReason`` per field.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Semantic version of the public GraphQL contract.
API_VERSION = "1.0.0"

#: Lifecycle of the published schema.
SCHEMA_STABILITY = "beta"

#: How long a deprecated member is supported before removal (advisory, in the docs).
DEPRECATION_WINDOW = "Deprecated members are supported for at least one minor version."


@dataclass(frozen=True, slots=True)
class Deprecation:
    """A deprecated schema member + its lifecycle metadata."""

    coordinate: str  # e.g. "Book.legacyStatus" or "Query.allBooks"
    reason: str
    since: str
    planned_removal: str | None = None

    def to_dict(self) -> dict[str, str | None]:
        return {
            "coordinate": self.coordinate,
            "reason": self.reason,
            "since": self.since,
            "plannedRemoval": self.planned_removal,
        }


#: The live deprecation registry. Members listed here MUST also carry a
#: ``deprecation_reason`` on their schema Field (so introspection/SDL agree).
DEPRECATIONS: tuple[Deprecation, ...] = (
    Deprecation(
        coordinate="Book.legacyId",
        reason="Use `id` (the canonical book id). `legacyId` is an alias kept for v0 clients.",
        since="1.0.0",
        planned_removal="2.0.0",
    ),
)


def deprecation_reason(coordinate: str) -> str | None:
    """The deprecation reason for a schema coordinate, or ``None`` if not deprecated."""
    for dep in DEPRECATIONS:
        if dep.coordinate == coordinate:
            return dep.reason
    return None


def api_version_payload() -> dict[str, object]:
    """The runtime ``ApiVersion`` value returned by ``Query.apiVersion``."""
    return {
        "version": API_VERSION,
        "stability": SCHEMA_STABILITY,
        "deprecationWindow": DEPRECATION_WINDOW,
        "deprecations": [d.to_dict() for d in DEPRECATIONS],
    }


__all__ = [
    "API_VERSION",
    "DEPRECATIONS",
    "DEPRECATION_WINDOW",
    "SCHEMA_STABILITY",
    "Deprecation",
    "api_version_payload",
    "deprecation_reason",
]
