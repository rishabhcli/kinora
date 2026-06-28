"""The evaluation context — the attribute bag a flag is evaluated against.

A :class:`EvalContext` carries the *unit of bucketing* (``key`` — usually a user
id, but it can be a tenant, a session, a device) plus arbitrary typed attributes
that targeting clauses match against (``plan="pro"``, ``country="US"``,
``app_version="2.4.1"``, ``beta=True``).

The context is deliberately a plain immutable value, not tied to the ORM ``User``
row, so the pure evaluator stays infra-free and any caller — a worker, a test, an
SDK embed — can build one from whatever identity it has.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

#: Attribute values we support in targeting. Lists allow multi-valued attributes
#: (e.g. ``groups=["beta", "internal"]``) matched by ``contains`` / ``in``.
AttrValue = str | int | float | bool | list[str] | None


@dataclass(frozen=True, slots=True)
class EvalContext:
    """An immutable identity + attribute bag for a single evaluation.

    ``key`` is the canonical bucketing unit. ``kind`` names what that key
    identifies (``"user"``, ``"tenant"``, ``"session"``) and lets a single
    context optionally carry secondary units in ``units`` so a flag can bucket on
    a *different* attribute than the primary key (e.g. bucket by ``tenant`` for a
    tenant-wide rollout while the primary key is a user).
    """

    key: str
    kind: str = "user"
    attributes: Mapping[str, AttrValue] = field(default_factory=dict)
    units: Mapping[str, str] = field(default_factory=dict)
    #: Anonymous contexts still bucket deterministically but are excluded from
    #: experiment exposure logging by default (no durable identity to attribute).
    anonymous: bool = False

    def get(self, name: str, default: AttrValue = None) -> AttrValue:
        """Return attribute ``name`` (``key``/``kind`` are addressable too)."""
        if name == "key":
            return self.key
        if name == "kind":
            return self.kind
        return self.attributes.get(name, default)

    def unit_for(self, bucket_by: str | None) -> str:
        """Resolve the bucketing unit for a rollout/experiment.

        ``None`` or ``"key"`` → the primary ``key``. A name present in ``units``
        → that secondary unit (e.g. bucket a user-keyed context by its tenant).
        Otherwise the value of the named attribute, coerced to ``str``; if that
        attribute is absent we fall back to the primary key so evaluation stays
        total (an unbucketable context simply buckets on its identity).
        """
        if bucket_by in (None, "", "key"):
            return self.key
        assert bucket_by is not None
        if bucket_by in self.units:
            return self.units[bucket_by]
        value = self.attributes.get(bucket_by)
        if value is None or isinstance(value, list):
            return self.key
        return str(value)

    def with_attributes(self, **overrides: AttrValue) -> EvalContext:
        """Return a copy with ``overrides`` merged over the attributes."""
        merged: dict[str, AttrValue] = {**self.attributes, **overrides}
        return EvalContext(
            key=self.key,
            kind=self.kind,
            attributes=merged,
            units=self.units,
            anonymous=self.anonymous,
        )

    @classmethod
    def of(cls, key: str, **attributes: AttrValue) -> EvalContext:
        """Ergonomic constructor: ``EvalContext.of("u1", plan="pro")``."""
        return cls(key=key, attributes=dict(attributes))

    def to_dict(self) -> dict[str, Any]:
        """A JSON-safe snapshot (for exposure logging / audit)."""
        return {
            "key": self.key,
            "kind": self.kind,
            "attributes": dict(self.attributes),
            "units": dict(self.units),
            "anonymous": self.anonymous,
        }


__all__ = ["AttrValue", "EvalContext"]
