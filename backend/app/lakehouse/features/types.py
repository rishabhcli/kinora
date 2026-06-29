"""The feature-store contract — entities, feature specs, views, services, refs.

These are the pure value objects every other module speaks. They are immutable
(frozen dataclasses) and carry no I/O, so the registry, the point-in-time join,
the stores, and the parity checker all share one vocabulary.

Vocabulary (Feast-style, adapted to Kinora):

* :class:`Entity` — the *join key* a feature is keyed on (a ``user``, a ``book``,
  a ``shot``). An entity has a name and a join-key column name.
* :class:`ValueType` — the wire dtype of a feature value, used for default
  coercion, online (de)serialisation, and parity bucketing.
* :class:`FeatureSpec` — one named feature: its dtype, an optional default for
  missing values, and a free description.
* :class:`FeatureView` — a named, versioned group of features that share an
  entity set, a source, and a TTL. The TTL is the freshness contract: a feature
  value older than ``ttl`` relative to the event timestamp is *stale* and must
  not be served (point-in-time correctness, §8.5 "scope a fact to the interval
  where it was true").
* :class:`OnDemandFeatureView` — a row-level transform computed at request time
  from request data + other features (no stored state). The streaming/on-demand
  computation seam.
* :class:`FeatureService` — a named bundle of feature references that a model
  consumes as a unit (the training/serving contract for one model).
* :class:`FeatureRef` — a ``view:feature`` (optionally ``view:feature@version``)
  reference resolvable against the registry.

Versioning is **content-addressed**: a feature view's ``version`` is derived from
its definition (entities + feature specs + ttl + source + transformation), so two
structurally identical definitions share a version and any change mints a new one
(see :mod:`app.lakehouse.features.registry`).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import timedelta
from enum import StrEnum

# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #


class FeatureStoreError(Exception):
    """Base class for every feature-store error."""


class DefinitionError(FeatureStoreError):
    """A registry definition is malformed or conflicts with an existing one."""


class ReferenceError(FeatureStoreError):
    """A feature reference does not resolve against the registry."""


class PointInTimeError(FeatureStoreError):
    """A point-in-time join precondition was violated (e.g. a missing key)."""


# --------------------------------------------------------------------------- #
# Value types
# --------------------------------------------------------------------------- #


class ValueType(StrEnum):
    """The wire dtype of a feature value.

    Used for default coercion, online store (de)serialisation, and choosing the
    right skew statistic in the parity checker (numeric → PSI/KS; categorical →
    L-infinity over the category distribution).
    """

    INT = "int"
    FLOAT = "float"
    BOOL = "bool"
    STRING = "string"
    BYTES = "bytes"
    #: A fixed-length float vector (e.g. the §8 1152-d canon embedding).
    FLOAT_VECTOR = "float_vector"
    #: A JSON-serialisable list (tags/genres).
    STRING_LIST = "string_list"

    @property
    def is_numeric(self) -> bool:
        """Whether a scalar of this type participates in numeric drift stats."""
        return self in (ValueType.INT, ValueType.FLOAT, ValueType.BOOL)

    @property
    def is_categorical(self) -> bool:
        """Whether values of this type are compared by category distribution."""
        return self in (ValueType.STRING,)


_VALID_NAME = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


def _check_name(name: str, *, kind: str) -> str:
    """Validate an identifier (entity/feature/view/service name)."""
    if not name or not _VALID_NAME.match(name):
        raise DefinitionError(
            f"{kind} name {name!r} must match {_VALID_NAME.pattern} "
            "(letters, digits, underscores; not starting with a digit)"
        )
    return name


# --------------------------------------------------------------------------- #
# Entity
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Entity:
    """A join key a feature view is keyed on.

    ``name`` is the logical entity (``user``); ``join_key`` is the column carrying
    its id in entity/feature frames (defaults to ``{name}_id``). ``value_type`` is
    the id's dtype (string ids are the Kinora default — see ``StrIdMixin``).
    """

    name: str
    join_key: str = ""
    value_type: ValueType = ValueType.STRING
    description: str = ""

    def __post_init__(self) -> None:
        _check_name(self.name, kind="entity")
        if not self.join_key:
            object.__setattr__(self, "join_key", f"{self.name}_id")
        _check_name(self.join_key, kind="entity join_key")

    def fingerprint(self) -> tuple[object, ...]:
        """Order-stable identity tuple used for content-addressed versioning."""
        return (self.name, self.join_key, self.value_type.value)


# --------------------------------------------------------------------------- #
# Feature spec
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class FeatureSpec:
    """One named feature: dtype + optional default + description."""

    name: str
    dtype: ValueType
    #: Value substituted when the feature is missing/expired for an entity. ``None``
    #: means "leave null" — callers must tolerate a null for that feature.
    default: object | None = None
    description: str = ""

    def __post_init__(self) -> None:
        _check_name(self.name, kind="feature")

    def fingerprint(self) -> tuple[object, ...]:
        return (self.name, self.dtype.value, _hashable(self.default))


def _hashable(value: object) -> object:
    """Coerce a default to a hashable, order-stable shape for fingerprinting."""
    if isinstance(value, (list, tuple)):
        return tuple(_hashable(v) for v in value)
    if isinstance(value, Mapping):
        return tuple(sorted((k, _hashable(v)) for k, v in value.items()))
    return value


# --------------------------------------------------------------------------- #
# Data sources (declarative; the store binds them to a concrete reader)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class FeatureSource:
    """Where a feature view's historical rows live (a logical table/topic name).

    ``timestamp_field`` is the *event time* column the point-in-time join sorts and
    merges on; ``created_field`` (optional) is the ingestion/arrival time used for
    late-arriving-data tie-breaking. ``kind`` distinguishes a batch/offline source
    from a streaming/push source (the streaming computation seam).
    """

    name: str
    timestamp_field: str = "event_timestamp"
    created_field: str | None = None
    kind: str = "batch"  # "batch" | "stream" | "push" | "request"

    def __post_init__(self) -> None:
        _check_name(self.name, kind="source")
        _check_name(self.timestamp_field, kind="source timestamp_field")
        if self.created_field is not None:
            _check_name(self.created_field, kind="source created_field")
        if self.kind not in ("batch", "stream", "push", "request"):
            raise DefinitionError(f"unknown source kind {self.kind!r}")

    def fingerprint(self) -> tuple[object, ...]:
        return (self.name, self.timestamp_field, self.created_field, self.kind)


# --------------------------------------------------------------------------- #
# Transformation (declarative metadata for lineage; execution lives elsewhere)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Transformation:
    """Declarative description of how a view's features are computed.

    This is *metadata* for lineage and versioning — the registry never executes
    it. ``expression`` is a human/engine-readable transform spec; ``inputs`` are
    the upstream column/feature names it reads. Changing a transform mints a new
    feature-view version, which is exactly what triggers a re-materialisation.
    """

    name: str
    expression: str = ""
    inputs: tuple[str, ...] = ()
    mode: str = "batch"  # "batch" | "on_demand" | "stream"

    def fingerprint(self) -> tuple[object, ...]:
        return (self.name, self.expression, tuple(self.inputs), self.mode)


# --------------------------------------------------------------------------- #
# Feature view
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class FeatureView:
    """A versioned group of features sharing entities, a source, and a TTL.

    The **TTL** is the freshness contract used by the point-in-time join: a stored
    feature value with event time ``t`` is valid for serving/joining at request
    time ``r`` only when ``r - ttl <= t <= r`` (a value older than ``ttl`` is stale
    and yields the feature's default). ``ttl is None`` means "never expires".

    ``version`` is assigned by the registry from :meth:`fingerprint` — leave it 0
    when constructing; ``register_feature_view`` stamps the content-addressed
    version. Two structurally identical views collapse to one version.
    """

    name: str
    entities: tuple[Entity, ...]
    features: tuple[FeatureSpec, ...]
    source: FeatureSource
    ttl: timedelta | None = None
    transformation: Transformation | None = None
    online: bool = True
    tags: Mapping[str, str] = field(default_factory=dict)
    description: str = ""
    version: int = 0
    owner: str = ""

    def __post_init__(self) -> None:
        _check_name(self.name, kind="feature_view")
        if not self.entities:
            raise DefinitionError(f"feature view {self.name!r} has no entities")
        if not self.features:
            raise DefinitionError(f"feature view {self.name!r} has no features")
        seen: set[str] = set()
        for feat in self.features:
            if feat.name in seen:
                raise DefinitionError(
                    f"feature view {self.name!r} declares duplicate feature {feat.name!r}"
                )
            seen.add(feat.name)
        if self.ttl is not None and self.ttl <= timedelta(0):
            raise DefinitionError(f"feature view {self.name!r} ttl must be positive")

    @property
    def join_keys(self) -> tuple[str, ...]:
        """The entity join-key columns this view is keyed on (stable order)."""
        return tuple(e.join_key for e in self.entities)

    @property
    def feature_names(self) -> tuple[str, ...]:
        return tuple(f.name for f in self.features)

    def feature(self, name: str) -> FeatureSpec:
        for f in self.features:
            if f.name == name:
                return f
        raise ReferenceError(f"feature view {self.name!r} has no feature {name!r}")

    def with_version(self, version: int) -> FeatureView:
        return replace(self, version=version)

    def fingerprint(self) -> tuple[object, ...]:
        """Content identity (excludes ``version`` — that *is* the fingerprint)."""
        return (
            self.name,
            tuple(e.fingerprint() for e in self.entities),
            tuple(f.fingerprint() for f in self.features),
            self.source.fingerprint(),
            None if self.ttl is None else self.ttl.total_seconds(),
            None if self.transformation is None else self.transformation.fingerprint(),
            self.online,
        )


# --------------------------------------------------------------------------- #
# On-demand feature view (row-level transform at request time)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class OnDemandFeatureView:
    """A row-level transform computed at request time (no stored state).

    Reads ``source_views`` (other feature views' values for the same row) and/or
    request-time inputs, and emits ``features``. The actual transform callable is
    held in the registry's on-demand registry, not on this metadata object, so the
    type stays a pure value object. The computation seam for derived/streaming
    features that must reflect request data (e.g. ``days_since_last_read`` from a
    stored ``last_read_ts`` and the request ``now``).
    """

    name: str
    features: tuple[FeatureSpec, ...]
    source_views: tuple[str, ...] = ()
    request_inputs: tuple[str, ...] = ()
    description: str = ""

    def __post_init__(self) -> None:
        _check_name(self.name, kind="on_demand_view")
        if not self.features:
            raise DefinitionError(f"on-demand view {self.name!r} has no features")

    @property
    def feature_names(self) -> tuple[str, ...]:
        return tuple(f.name for f in self.features)


# --------------------------------------------------------------------------- #
# Feature service (the per-model bundle of references)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class FeatureService:
    """A named bundle of feature references a model consumes as a unit.

    ``features`` are :class:`FeatureRef` (``view:feature``) strings. A model
    pins a feature service; the service resolves to a stable column order for both
    the training set and the online vector, so training/serving cannot drift in
    *which* features (or their order) they see.
    """

    name: str
    features: tuple[str, ...]
    description: str = ""
    owner: str = ""
    tags: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _check_name(self.name, kind="feature_service")
        if not self.features:
            raise DefinitionError(f"feature service {self.name!r} has no features")

    def refs(self) -> tuple[FeatureRef, ...]:
        return tuple(FeatureRef.parse(r) for r in self.features)


# --------------------------------------------------------------------------- #
# Feature reference
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class FeatureRef:
    """A ``view:feature`` (optionally ``view:feature@version``) reference."""

    view: str
    feature: str
    version: int | None = None

    @classmethod
    def parse(cls, ref: str) -> FeatureRef:
        """Parse ``"view:feature"`` or ``"view:feature@3"``."""
        head, _, version_s = ref.partition("@")
        view, sep, feature = head.partition(":")
        if not sep or not view or not feature:
            raise ReferenceError(
                f"feature reference {ref!r} must be 'view:feature' (optionally '@version')"
            )
        version: int | None = None
        if version_s:
            try:
                version = int(version_s)
            except ValueError as exc:  # pragma: no cover - defensive
                raise ReferenceError(f"bad version in feature reference {ref!r}") from exc
        return cls(view=view.strip(), feature=feature.strip(), version=version)

    @property
    def column(self) -> str:
        """The output column name for this reference (``view__feature``)."""
        return f"{self.view}__{self.feature}"

    def __str__(self) -> str:
        base = f"{self.view}:{self.feature}"
        return f"{base}@{self.version}" if self.version is not None else base


def parse_refs(refs: Sequence[str | FeatureRef]) -> tuple[FeatureRef, ...]:
    """Coerce a heterogeneous sequence of refs to :class:`FeatureRef` objects."""
    out: list[FeatureRef] = []
    for r in refs:
        out.append(r if isinstance(r, FeatureRef) else FeatureRef.parse(r))
    return tuple(out)


__all__ = [
    "DefinitionError",
    "Entity",
    "FeatureRef",
    "FeatureService",
    "FeatureSource",
    "FeatureSpec",
    "FeatureStoreError",
    "FeatureView",
    "OnDemandFeatureView",
    "PointInTimeError",
    "ReferenceError",
    "Transformation",
    "ValueType",
    "parse_refs",
]
