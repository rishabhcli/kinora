"""The schema registry — register message schemas with semver + content hash.

Every inter-service message schema is registered here under its ``schema_id`` and
:class:`~app.servicemesh.versioning.SemVer`. A channel declares a
:class:`~app.servicemesh.compatibility.CompatibilityMode`, and the registry runs
the CI gate on every *evolution* (registering a new version of an existing id),
rejecting a breaking change to a stable channel before it can ever be emitted.

Registration is idempotent on *content*: re-registering the identical schema (same
content hash) is a no-op, but registering a *different* shape under an already-used
``(schema_id, version)`` is a programming error and raises.

The registry is the lookup the consumer dispatcher and the negotiator both consult:
"what versions of ``shot.render.job`` do I know, and what are their shapes?"
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

import structlog

from app.servicemesh.compatibility import CompatibilityMode, assert_evolution_allowed
from app.servicemesh.errors import (
    SchemaAlreadyRegisteredError,
    SchemaHashMismatchError,
    SchemaNotFoundError,
)
from app.servicemesh.schema import MessageSchema
from app.servicemesh.versioning import SemVer

__all__ = ["RegisteredSchema", "ChannelInfo", "SchemaRegistry"]

log = structlog.get_logger("app.servicemesh.registry")


@dataclass(frozen=True, slots=True)
class RegisteredSchema:
    """A schema as stored in the registry, with its computed content hash."""

    schema: MessageSchema
    content_hash: str

    @property
    def schema_id(self) -> str:
        return self.schema.schema_id

    @property
    def version(self) -> SemVer:
        return self.schema.version


@dataclass(slots=True)
class ChannelInfo:
    """Per-``schema_id`` channel metadata + its version history."""

    schema_id: str
    compatibility: CompatibilityMode
    versions: dict[SemVer, RegisteredSchema] = field(default_factory=dict)

    @property
    def latest(self) -> RegisteredSchema:
        return self.versions[max(self.versions)]

    def sorted_versions(self) -> list[SemVer]:
        return sorted(self.versions)


class SchemaRegistry:
    """A thread-safe registry of versioned message schemas + compatibility gate."""

    def __init__(self) -> None:
        self._channels: dict[str, ChannelInfo] = {}
        self._lock = threading.RLock()

    # -- channels ----------------------------------------------------------- #
    def declare_channel(
        self, schema_id: str, compatibility: CompatibilityMode = CompatibilityMode.BACKWARD
    ) -> ChannelInfo:
        """Declare (or fetch) a channel and its compatibility contract.

        Re-declaring with a *different* mode is rejected — the contract a channel
        is held to must not silently weaken.
        """
        with self._lock:
            existing = self._channels.get(schema_id)
            if existing is None:
                channel = ChannelInfo(schema_id=schema_id, compatibility=compatibility)
                self._channels[schema_id] = channel
                return channel
            if existing.compatibility != compatibility:
                raise SchemaAlreadyRegisteredError(
                    f"channel {schema_id!r} already declared with compatibility "
                    f"{existing.compatibility.value!r}, cannot redeclare as "
                    f"{compatibility.value!r}"
                )
            return existing

    # -- registration ------------------------------------------------------- #
    def register(
        self,
        schema: MessageSchema,
        *,
        compatibility: CompatibilityMode | None = None,
        stable_only: bool = True,
    ) -> RegisteredSchema:
        """Register a schema version, running the compatibility gate on evolution.

        * First version of an id -> declares the channel (with ``compatibility`` or
          the BACKWARD default) and stores it.
        * New version of an existing id -> the CI gate
          (:func:`assert_evolution_allowed`) runs against the *immediately
          preceding* version; a breaking change to a stable channel raises
          :class:`~app.servicemesh.errors.BreakingChangeError`.
        * Same ``(id, version)`` re-registered -> idempotent iff the content hash
          matches, else :class:`SchemaAlreadyRegisteredError`.
        """
        content_hash = schema.content_hash()
        entry = RegisteredSchema(schema=schema, content_hash=content_hash)

        with self._lock:
            channel = self._channels.get(schema.schema_id)
            if channel is None:
                channel = self.declare_channel(
                    schema.schema_id, compatibility or CompatibilityMode.BACKWARD
                )
            elif compatibility is not None and compatibility != channel.compatibility:
                raise SchemaAlreadyRegisteredError(
                    f"channel {schema.schema_id!r} compatibility is "
                    f"{channel.compatibility.value!r}; refusing implicit change to "
                    f"{compatibility.value!r}"
                )

            existing = channel.versions.get(schema.version)
            if existing is not None:
                if existing.content_hash != content_hash:
                    raise SchemaAlreadyRegisteredError(
                        f"{schema.schema_id}@{schema.version} already registered with "
                        f"a different shape ({existing.content_hash} != {content_hash})"
                    )
                return existing  # idempotent re-registration

            # Evolution gate: compare to the highest version strictly below this one.
            predecessors = [v for v in channel.versions if v < schema.version]
            if predecessors:
                prior = channel.versions[max(predecessors)]
                assert_evolution_allowed(
                    prior.schema,
                    schema,
                    channel.compatibility,
                    stable_only=stable_only,
                )

            channel.versions[schema.version] = entry
            log.debug(
                "servicemesh.schema.registered",
                schema_id=schema.schema_id,
                version=str(schema.version),
                content_hash=content_hash,
                compatibility=channel.compatibility.value,
            )
            return entry

    # -- lookup ------------------------------------------------------------- #
    def get(self, schema_id: str, version: SemVer | str) -> RegisteredSchema:
        """Fetch an exact ``(schema_id, version)`` or raise."""
        version = SemVer.coerce(version)
        with self._lock:
            channel = self._channels.get(schema_id)
            if channel is None or version not in channel.versions:
                raise SchemaNotFoundError(f"{schema_id}@{version} is not registered")
            return channel.versions[version]

    def latest(self, schema_id: str) -> RegisteredSchema:
        """Fetch the highest registered version of ``schema_id`` or raise."""
        with self._lock:
            channel = self._channels.get(schema_id)
            if channel is None or not channel.versions:
                raise SchemaNotFoundError(f"no versions registered for {schema_id!r}")
            return channel.latest

    def versions(self, schema_id: str) -> list[SemVer]:
        """All registered versions of ``schema_id`` (ascending), or raise."""
        with self._lock:
            channel = self._channels.get(schema_id)
            if channel is None:
                raise SchemaNotFoundError(f"unknown schema id {schema_id!r}")
            return channel.sorted_versions()

    def has(self, schema_id: str, version: SemVer | str | None = None) -> bool:
        """Membership test for an id (optionally at a specific version)."""
        with self._lock:
            channel = self._channels.get(schema_id)
            if channel is None:
                return False
            if version is None:
                return bool(channel.versions)
            return SemVer.coerce(version) in channel.versions

    def channel(self, schema_id: str) -> ChannelInfo:
        """The channel metadata for ``schema_id`` or raise."""
        with self._lock:
            channel = self._channels.get(schema_id)
            if channel is None:
                raise SchemaNotFoundError(f"unknown schema id {schema_id!r}")
            return channel

    def schema_ids(self) -> list[str]:
        """All known schema ids (sorted)."""
        with self._lock:
            return sorted(self._channels)

    def verify_hashes(self) -> None:
        """Re-derive every stored content hash and raise on the first mismatch.

        A cheap integrity audit (used by a startup self-check): catches a stored
        entry whose recorded hash no longer matches its recomputed canonical form.
        """
        with self._lock:
            for channel in self._channels.values():
                for entry in channel.versions.values():
                    fresh = entry.schema.content_hash()
                    if fresh != entry.content_hash:
                        raise SchemaHashMismatchError(
                            f"{entry.schema_id}@{entry.version}: stored "
                            f"{entry.content_hash} != recomputed {fresh}"
                        )
