"""The capability-permission model — the least-privilege vocabulary.

A **capability** is a single named authority a plugin may request, e.g.
``canon.read`` or ``net.fetch``. Capabilities are *hierarchical, dotted scopes*:
a grant of ``canon`` implies ``canon.read`` and ``canon.write``; a grant of
``canon.read`` does **not** imply ``canon.write``. This mirrors OAuth-style
scope trees and lets a manifest ask for exactly what it needs.

Three pure pieces live here, all I/O-free and deterministic:

* :class:`Capability` — a parsed, validated dotted scope with hierarchy logic
  (``implies`` / ``covered_by``).
* :data:`CAPABILITY_CATALOG` — the closed universe of capabilities the host
  knows how to broker, each tagged with a :class:`RiskTier`. A manifest may only
  request capabilities the catalog knows; an unknown scope is rejected at
  authoring time (you cannot smuggle authority by inventing a scope).
* :class:`GrantSet` — an immutable set of *granted* capabilities with the single
  hot-path predicate :meth:`permits`, used by the broker on every host call.

The design rule: **deny by default.** An empty :class:`GrantSet` permits
nothing; a capability is permitted only if some grant in the set covers it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from app.platform.plugins.errors import PluginValidationError

#: A capability scope is one or more lowercase dotted segments.
_SCOPE_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$")


class RiskTier(StrEnum):
    """How dangerous a capability is — drives review policy and default grants.

    * ``LOW`` — read-only / metadata access; auto-grantable.
    * ``MEDIUM`` — writes scoped to the plugin's own subjects; reviewable.
    * ``HIGH`` — broad writes, outbound network, secret access; manual review.
    """

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"

    @property
    def rank(self) -> int:
        """Numeric ordering (LOW < MEDIUM < HIGH) for max/threshold comparisons."""
        return {"low": 0, "medium": 1, "high": 2}[self.value]


@dataclass(frozen=True, slots=True)
class Capability:
    """A parsed, validated dotted capability scope (e.g. ``canon.read``)."""

    scope: str

    def __post_init__(self) -> None:
        if not isinstance(self.scope, str) or not _SCOPE_RE.match(self.scope):
            raise PluginValidationError(f"invalid capability scope: {self.scope!r}")

    @property
    def segments(self) -> tuple[str, ...]:
        """The scope split into its dotted segments."""
        return tuple(self.scope.split("."))

    @property
    def root(self) -> str:
        """The top-level segment (the resource family, e.g. ``canon``)."""
        return self.segments[0]

    def implies(self, other: Capability) -> bool:
        """True when granting ``self`` also grants ``other`` (``self`` is a prefix).

        ``canon`` implies ``canon.read``; ``canon.read`` implies itself but not
        ``canon.write``. Equality is the degenerate prefix.
        """
        if self.scope == other.scope:
            return True
        return other.scope.startswith(self.scope + ".")

    def covered_by(self, grants: GrantSet) -> bool:
        """True when some capability in ``grants`` implies this one."""
        return grants.permits(self)

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.scope


@dataclass(frozen=True, slots=True)
class CapabilitySpec:
    """Catalog metadata for one known capability scope."""

    scope: str
    risk: RiskTier
    description: str

    @property
    def capability(self) -> Capability:
        return Capability(self.scope)


def _spec(scope: str, risk: RiskTier, description: str) -> CapabilitySpec:
    return CapabilitySpec(scope=scope, risk=risk, description=description)


#: The closed universe of capabilities the host can broker. A manifest may only
#: request a scope that the catalog *knows* (exact match or a known parent).
CAPABILITY_CATALOG: dict[str, CapabilitySpec] = {
    s.scope: s
    for s in (
        # --- Canon / memory (the §8 MCP canon surface) ---
        _spec("canon.read", RiskTier.LOW, "Read canon entities and states for a beat."),
        _spec("canon.query", RiskTier.LOW, "Run the §8.3 canon.query retrieval policy."),
        _spec("canon.write", RiskTier.HIGH, "Upsert canon entities / assert / retire states."),
        # --- Episodic / search ---
        _spec("episodic.search", RiskTier.LOW, "Nearest prior accepted shots for a beat."),
        _spec("episodic.log", RiskTier.MEDIUM, "Append a QA / outcome episodic record."),
        # --- Books / library (read-only projections) ---
        _spec("book.read", RiskTier.LOW, "Read book metadata, pages, and source spans."),
        _spec("shot.read", RiskTier.LOW, "Read shot specs / status / results."),
        # --- Render pipeline hooks ---
        _spec("render.read", RiskTier.LOW, "Inspect a render job / shot spec in a hook."),
        _spec("render.annotate", RiskTier.MEDIUM, "Attach metadata/labels to a render artifact."),
        # --- Key/value scratch store scoped to the plugin ---
        _spec("storage.kv.read", RiskTier.LOW, "Read the plugin's own scoped key/value store."),
        _spec("storage.kv.write", RiskTier.MEDIUM, "Write the plugin's own scoped key/value."),
        # --- Outbound network (host-mediated, allowlisted) ---
        _spec("net.fetch", RiskTier.HIGH, "Make a host-mediated outbound HTTP request."),
        # --- Logging / telemetry (always safe) ---
        _spec("log.write", RiskTier.LOW, "Emit a structured log line via the host."),
        # --- Secrets (host-held credentials, never raw) ---
        _spec("secrets.read", RiskTier.HIGH, "Read a named host-managed secret value."),
    )
}

#: Capability roots present in the catalog — used to validate parent-scope grants
#: (e.g. a manifest asking for ``canon`` is valid because ``canon.*`` exists).
_CATALOG_ROOTS: frozenset[str] = frozenset(s.split(".")[0] for s in CAPABILITY_CATALOG)


def is_known_capability(scope: str) -> bool:
    """True when ``scope`` is a catalog capability or a parent of one.

    A parent grant (``canon``) is allowed because it is a legitimate, if broad,
    request that covers known children. A scope with no known descendant
    (``filesystem.write``) is rejected — you cannot request authority the host
    has no broker for.
    """
    try:
        cap = Capability(scope)
    except PluginValidationError:
        return False
    if cap.scope in CAPABILITY_CATALOG:
        return True
    prefix = cap.scope + "."
    return any(known.startswith(prefix) for known in CAPABILITY_CATALOG)


def expand_catalog(scope: str) -> tuple[CapabilitySpec, ...]:
    """All catalog specs covered by a (possibly parent) grant ``scope``."""
    cap = Capability(scope)
    return tuple(spec for spec in CAPABILITY_CATALOG.values() if cap.implies(spec.capability))


def risk_of(scope: str) -> RiskTier:
    """The maximum :class:`RiskTier` among the catalog capabilities a grant covers.

    A parent grant inherits the highest risk of any child it subsumes (granting
    ``canon`` is at least as dangerous as ``canon.write``).
    """
    covered = expand_catalog(scope)
    if not covered:
        raise PluginValidationError(f"unknown capability: {scope!r}")
    return max((spec.risk for spec in covered), key=lambda r: r.rank)


@dataclass(frozen=True, slots=True)
class GrantSet:
    """An immutable set of *granted* capabilities; the broker's authority oracle.

    ``permits`` is the single hot-path predicate. Construction validates and
    *normalizes* the grants: every scope must be known to the catalog, and a
    grant subsumed by a broader sibling is dropped (``canon`` absorbs
    ``canon.read``) so the stored set is minimal and comparisons are stable.
    """

    grants: frozenset[str]

    @classmethod
    def of(cls, *scopes: str) -> GrantSet:
        """Build a normalized grant set from raw scopes (deny-by-default empty)."""
        return cls.from_iterable(scopes)

    @classmethod
    def from_iterable(cls, scopes: object) -> GrantSet:
        """Build from any iterable of scope strings, validating + normalizing."""
        if isinstance(scopes, str) or not hasattr(scopes, "__iter__"):
            raise PluginValidationError("grants must be an iterable of scope strings")
        caps: list[Capability] = []
        for raw in scopes:
            if not isinstance(raw, str):
                raise PluginValidationError(f"capability must be a string: {raw!r}")
            if not is_known_capability(raw):
                raise PluginValidationError(f"unknown capability: {raw!r}")
            caps.append(Capability(raw))
        # Drop any capability implied by a *different* one in the set.
        minimal = {
            c.scope
            for c in caps
            if not any(other.scope != c.scope and other.implies(c) for other in caps)
        }
        return cls(grants=frozenset(minimal))

    def permits(self, capability: Capability | str) -> bool:
        """True iff some grant implies ``capability`` (deny-by-default otherwise)."""
        cap = capability if isinstance(capability, Capability) else Capability(capability)
        return any(Capability(g).implies(cap) for g in self.grants)

    def require(self, capability: Capability | str) -> None:
        """Raise :class:`CapabilityDeniedError` if ``capability`` is not permitted."""
        from app.platform.plugins.errors import CapabilityDeniedError

        cap = capability if isinstance(capability, Capability) else Capability(capability)
        if not self.permits(cap):
            raise CapabilityDeniedError(
                f"capability {cap.scope!r} is not granted to this plugin",
                capability=cap.scope,
            )

    @property
    def max_risk(self) -> RiskTier:
        """The highest risk tier across all grants (``LOW`` for the empty set)."""
        tier = RiskTier.LOW
        for g in self.grants:
            r = risk_of(g)
            if r.rank > tier.rank:
                tier = r
        return tier

    def is_subset_of(self, other: GrantSet) -> bool:
        """True when every capability here is also permitted by ``other``.

        Used by the host to confirm a *granted* set never exceeds what the
        installer/operator approved (a plugin cannot escalate beyond its grant).
        """
        return all(other.permits(g) for g in self.grants)

    def to_sorted(self) -> tuple[str, ...]:
        """The grants as a deterministically sorted tuple (for serialization)."""
        return tuple(sorted(self.grants))


#: The deny-everything grant set (the secure default for an ungranted runtime).
EMPTY_GRANTS = GrantSet(grants=frozenset())


__all__ = [
    "CAPABILITY_CATALOG",
    "Capability",
    "CapabilitySpec",
    "EMPTY_GRANTS",
    "GrantSet",
    "RiskTier",
    "expand_catalog",
    "is_known_capability",
    "risk_of",
]
