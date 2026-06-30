"""Capability / version negotiation between roles.

Two roles (an ``api`` producing render jobs, a ``render-worker`` consuming them)
must agree on a schema version both can speak. Each role advertises a
:class:`Capability` per ``schema_id``: the version range it can *produce* and the
range it can *consume*, plus a *preferred* version. The
:func:`negotiate` function intersects a producer's produce-range with a consumer's
consume-range and selects the highest commonly-supported version (preferring the
producer's preference when it lies in the overlap).

This is the handshake a fleet runs at startup (or on a config-version pubsub
broadcast) so a freshly-deployed producer doesn't emit a version older consumers
can't read — it negotiates down, or refuses with a clear
:class:`~app.servicemesh.errors.NegotiationError`.

Everything here is pure: capabilities are data, negotiation is a fold. A
:class:`CapabilityRegistry` keeps a process's advertised capabilities for the local
role so other components can publish them.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

from app.servicemesh.errors import NegotiationError
from app.servicemesh.roles import ProducerRole
from app.servicemesh.versioning import SemVer, VersionRange

__all__ = [
    "Capability",
    "RoleManifest",
    "NegotiationResult",
    "negotiate",
    "CapabilityRegistry",
]


@dataclass(frozen=True, slots=True)
class Capability:
    """What a role can do with one ``schema_id``."""

    schema_id: str
    produces: VersionRange | None = None
    consumes: VersionRange | None = None
    preferred: SemVer | None = None

    def __post_init__(self) -> None:
        if self.produces is None and self.consumes is None:
            raise NegotiationError(
                f"capability for {self.schema_id!r} declares neither produce nor "
                f"consume range"
            )
        # The preference, if any, must be expressible by this role.
        if self.preferred is not None:
            in_produce = self.produces is not None and self.produces.contains(self.preferred)
            in_consume = self.consumes is not None and self.consumes.contains(self.preferred)
            if not (in_produce or in_consume):
                raise NegotiationError(
                    f"preferred {self.preferred} for {self.schema_id!r} lies outside "
                    f"the role's own ranges"
                )


@dataclass(frozen=True, slots=True)
class RoleManifest:
    """A role's full set of advertised capabilities."""

    role: ProducerRole
    capabilities: tuple[Capability, ...] = field(default_factory=tuple)

    def by_schema(self, schema_id: str) -> Capability | None:
        for cap in self.capabilities:
            if cap.schema_id == schema_id:
                return cap
        return None


@dataclass(frozen=True, slots=True)
class NegotiationResult:
    """The agreed version for a producer->consumer pairing on one schema."""

    schema_id: str
    producer: ProducerRole
    consumer: ProducerRole
    agreed_version: SemVer
    overlap: VersionRange


def negotiate(
    producer: RoleManifest,
    consumer: RoleManifest,
    schema_id: str,
    *,
    known_versions: list[SemVer] | None = None,
) -> NegotiationResult:
    """Agree on the highest version a producer can emit and a consumer can read.

    The overlap is ``producer.produces ∩ consumer.consumes``. Within the overlap we
    select:

    1. the producer's ``preferred`` version, if it falls inside the overlap (and, if
       ``known_versions`` is given, is one of them);
    2. else the highest ``known_versions`` member inside the overlap;
    3. else the overlap's lower bound (the lowest commonly-supported version), which
       is always safe because the range is inclusive there.

    Raises :class:`NegotiationError` when either side lacks the schema or the ranges
    are disjoint.
    """
    prod_cap = producer.by_schema(schema_id)
    cons_cap = consumer.by_schema(schema_id)
    if prod_cap is None or prod_cap.produces is None:
        raise NegotiationError(
            f"{producer.role.value} does not produce {schema_id!r}"
        )
    if cons_cap is None or cons_cap.consumes is None:
        raise NegotiationError(
            f"{consumer.role.value} does not consume {schema_id!r}"
        )

    overlap = prod_cap.produces.intersect(cons_cap.consumes)
    if overlap is None:
        raise NegotiationError(
            f"no common version for {schema_id!r}: {producer.role.value} produces "
            f"{prod_cap.produces} but {consumer.role.value} consumes "
            f"{cons_cap.consumes}"
        )

    # 1. Producer preference, if it sits in the overlap (and is known, if a known
    # set was supplied).
    if (
        prod_cap.preferred is not None
        and overlap.contains(prod_cap.preferred)
        and (known_versions is None or prod_cap.preferred in known_versions)
    ):
        return NegotiationResult(
            schema_id=schema_id,
            producer=producer.role,
            consumer=consumer.role,
            agreed_version=prod_cap.preferred,
            overlap=overlap,
        )

    # 2. Highest known version inside the overlap.
    if known_versions:
        inside = sorted(v for v in known_versions if overlap.contains(v))
        if inside:
            return NegotiationResult(
                schema_id=schema_id,
                producer=producer.role,
                consumer=consumer.role,
                agreed_version=inside[-1],
                overlap=overlap,
            )

    # 3. The lower bound is always commonly supported.
    return NegotiationResult(
        schema_id=schema_id,
        producer=producer.role,
        consumer=consumer.role,
        agreed_version=overlap.min_inclusive,
        overlap=overlap,
    )


class CapabilityRegistry:
    """Holds a process's advertised capabilities, grouped by role.

    A thin, thread-safe store so the local role can publish what it speaks and a
    coordinator can negotiate pairings between any two known roles.
    """

    def __init__(self) -> None:
        self._manifests: dict[ProducerRole, dict[str, Capability]] = {}
        self._lock = threading.RLock()

    def advertise(self, role: ProducerRole, capability: Capability) -> None:
        """Record (or replace) one capability for ``role``."""
        with self._lock:
            self._manifests.setdefault(role, {})[capability.schema_id] = capability

    def manifest(self, role: ProducerRole) -> RoleManifest:
        """The full :class:`RoleManifest` advertised for ``role``."""
        with self._lock:
            caps = tuple(self._manifests.get(role, {}).values())
        return RoleManifest(role=role, capabilities=caps)

    def negotiate(
        self,
        producer: ProducerRole,
        consumer: ProducerRole,
        schema_id: str,
        *,
        known_versions: list[SemVer] | None = None,
    ) -> NegotiationResult:
        """Negotiate using the manifests advertised for two roles."""
        return negotiate(
            self.manifest(producer),
            self.manifest(consumer),
            schema_id,
            known_versions=known_versions,
        )
